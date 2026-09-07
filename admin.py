"""
admin.py
========
Admin-only router for managing API users, tracking users, and settlement
change reviews.

Authentication: admin_token HttpOnly cookie (JWT), issued by POST /admin/login.
Credentials are hardcoded for now — only one admin account exists.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import bet_tracking

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SECRET_KEY         = os.getenv("JWT_SECRET_KEY", "")
_ALGORITHM          = "HS256"
_ADMIN_TOKEN_EXPIRE = 8   # hours

# Hardcoded admin credentials (single admin account)
_ADMIN_EMAIL    = os.getenv("ADMIN_EMAIL")
_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _issue_admin_token() -> str:
    payload = {
        "sub":   _ADMIN_EMAIL,
        "admin": True,
        "exp":   datetime.utcnow() + timedelta(hours=_ADMIN_TOKEN_EXPIRE),
        "iat":   datetime.utcnow(),
    }
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def _verify_admin_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        if not payload.get("admin"):
            raise HTTPException(status_code=403, detail="Not an admin token")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Admin session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid admin token")


async def _require_admin(admin_token: str = Cookie(None)):
    if not admin_token:
        raise HTTPException(status_code=401, detail="Admin authentication required")
    if not _SECRET_KEY:
        raise HTTPException(status_code=500, detail="JWT_SECRET_KEY not configured")
    return _verify_admin_token(admin_token)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AdminLoginRequest(BaseModel):
    email:    str
    password: str


class CreateApiUserRequest(BaseModel):
    name:         str  = Field(..., min_length=1)
    email:        str  = Field(...)
    organization: str | None = Field(None)
    notes:        str | None = Field(None)


class AutoApproveRequest(BaseModel):
    enabled: bool


class ReviewDecisionRequest(BaseModel):
    admin_note: Optional[str] = Field(None, max_length=2000)


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/login")
async def admin_login(body: AdminLoginRequest, request: Request):
    if body.email.strip().lower() != _ADMIN_EMAIL.lower() or body.password != _ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    if not _SECRET_KEY:
        raise HTTPException(status_code=500, detail="JWT_SECRET_KEY not configured")

    token    = _issue_admin_token()
    origin   = request.headers.get("origin", "")
    is_local = "localhost" in origin or "127.0.0.1" in origin

    response = JSONResponse({"authenticated": True, "email": _ADMIN_EMAIL})
    response.set_cookie(
        key="admin_token",
        value=token,
        max_age=_ADMIN_TOKEN_EXPIRE * 3600,
        httponly=True,
        secure=not is_local,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def admin_logout(request: Request):
    response = JSONResponse({"message": "Admin logged out"})
    response.delete_cookie("admin_token")
    return response


@router.get("/check")
async def admin_check(admin_token: str = Cookie(None)):
    """Returns 200 if the admin cookie is valid, 401 otherwise."""
    if not admin_token or not _SECRET_KEY:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _verify_admin_token(admin_token)
    return {"authenticated": True, "email": _ADMIN_EMAIL}


# ---------------------------------------------------------------------------
# API user management
# ---------------------------------------------------------------------------

@router.get("/users")
async def list_api_users(admin=Depends(_require_admin)):
    users = await run_in_threadpool(bet_tracking.list_api_users)
    return {"users": users, "total": len(users)}


@router.post("/users", status_code=201)
async def create_api_user(body: CreateApiUserRequest, admin=Depends(_require_admin)):
    try:
        user = await run_in_threadpool(
            bet_tracking.create_api_user,
            body.name, body.email, body.organization, body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(status_code=409, detail="An API user with this email already exists")
        raise HTTPException(status_code=500, detail=str(exc))
    return user


@router.get("/users/{user_id}")
async def get_api_user(user_id: str, admin=Depends(_require_admin)):
    user = await run_in_threadpool(bet_tracking.get_api_user_by_id, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="API user not found")
    return user


@router.post("/users/{user_id}/regenerate-key")
async def regenerate_api_key(user_id: str, admin=Depends(_require_admin)):
    result = await run_in_threadpool(bet_tracking.regenerate_api_key, user_id)
    if not result:
        raise HTTPException(status_code=404, detail="API user not found")
    return result


@router.patch("/users/{user_id}/deactivate")
async def deactivate_api_user(user_id: str, admin=Depends(_require_admin)):
    ok = await run_in_threadpool(bet_tracking.set_api_user_active, user_id, False)
    if not ok:
        raise HTTPException(status_code=404, detail="API user not found")
    return {"deactivated": True, "user_id": user_id}


@router.patch("/users/{user_id}/activate")
async def activate_api_user(user_id: str, admin=Depends(_require_admin)):
    ok = await run_in_threadpool(bet_tracking.set_api_user_active, user_id, True)
    if not ok:
        raise HTTPException(status_code=404, detail="API user not found")
    return {"activated": True, "user_id": user_id}


@router.delete("/users/{user_id}")
async def delete_api_user(user_id: str, admin=Depends(_require_admin)):
    ok = await run_in_threadpool(bet_tracking.delete_api_user, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API user not found")
    return {"deleted": True, "user_id": user_id}


# ---------------------------------------------------------------------------
# Unified user management (cookie + API key) — auto-approve toggles
# ---------------------------------------------------------------------------

@router.get("/managed-users")
async def list_managed_users(
    auth_source: Optional[str] = Query(None, description="cookie | api_key"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin=Depends(_require_admin),
):
    if auth_source and auth_source not in (
        bet_tracking.AUTH_SOURCE_COOKIE,
        bet_tracking.AUTH_SOURCE_API_KEY,
    ):
        raise HTTPException(
            status_code=400,
            detail="auth_source must be 'cookie' or 'api_key'",
        )
    users, total = await run_in_threadpool(
        bet_tracking.list_managed_users,
        limit=limit,
        offset=offset,
        auth_source=auth_source,
    )
    return {
        "users": users,
        "total": total,
        "returned": len(users),
        "offset": offset,
        "limit": limit,
    }


@router.patch("/managed-users/{user_id}/auto-approve")
async def set_managed_user_auto_approve(
    user_id: str,
    body: AutoApproveRequest,
    admin=Depends(_require_admin),
):
    updated = await run_in_threadpool(
        bet_tracking.set_user_auto_approve, user_id, body.enabled
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    return updated


# ---------------------------------------------------------------------------
# Settlement change request reviews
# ---------------------------------------------------------------------------

@router.get("/settlement-requests")
async def list_settlement_requests(
    status: Optional[str] = Query(None, description="pending | approved | denied"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin=Depends(_require_admin),
):
    if status and status not in ("pending", "approved", "denied"):
        raise HTTPException(
            status_code=400,
            detail="status must be pending, approved, or denied",
        )
    requests, total = await run_in_threadpool(
        bet_tracking.list_settlement_change_requests,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "requests": requests,
        "total": total,
        "returned": len(requests),
        "offset": offset,
        "limit": limit,
    }


@router.get("/settlement-requests/{request_id}")
async def get_settlement_request(request_id: str, admin=Depends(_require_admin)):
    req = await run_in_threadpool(
        bet_tracking.get_settlement_change_request, request_id
    )
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    return req


@router.post("/settlement-requests/{request_id}/approve")
async def approve_settlement_request(
    request_id: str,
    body: ReviewDecisionRequest = ReviewDecisionRequest(),
    admin=Depends(_require_admin),
):
    reviewed_by = admin.get("sub") or _ADMIN_EMAIL or "admin"
    try:
        result = await run_in_threadpool(
            bet_tracking.approve_settlement_change_request,
            request_id,
            reviewed_by=reviewed_by,
            admin_note=body.admin_note,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "Request not found":
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)
    return result


@router.post("/settlement-requests/{request_id}/deny")
async def deny_settlement_request(
    request_id: str,
    body: ReviewDecisionRequest = ReviewDecisionRequest(),
    admin=Depends(_require_admin),
):
    reviewed_by = admin.get("sub") or _ADMIN_EMAIL or "admin"
    try:
        result = await run_in_threadpool(
            bet_tracking.deny_settlement_change_request,
            request_id,
            reviewed_by=reviewed_by,
            admin_note=body.admin_note,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "Request not found":
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)
    return result
