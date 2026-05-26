"""
admin.py
========
Admin-only router for managing API users and their access keys.

Authentication: admin_token HttpOnly cookie (JWT), issued by POST /admin/login.
Credentials are hardcoded for now — only one admin account exists.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Cookie, HTTPException, Request
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
_ADMIN_EMAIL    = "admin.eternitylabs@gmail.com"
_ADMIN_PASSWORD = "Admin123*#"

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
async def list_api_users(admin=None):
    admin  # dependency not needed here — route guarded by include_router dep
    users = await run_in_threadpool(bet_tracking.list_api_users)
    return {"users": users, "total": len(users)}


@router.post("/users", status_code=201)
async def create_api_user(body: CreateApiUserRequest):
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
async def get_api_user(user_id: str):
    user = await run_in_threadpool(bet_tracking.get_api_user_by_id, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="API user not found")
    return user


@router.post("/users/{user_id}/regenerate-key")
async def regenerate_api_key(user_id: str):
    result = await run_in_threadpool(bet_tracking.regenerate_api_key, user_id)
    if not result:
        raise HTTPException(status_code=404, detail="API user not found")
    return result


@router.patch("/users/{user_id}/deactivate")
async def deactivate_api_user(user_id: str):
    ok = await run_in_threadpool(bet_tracking.set_api_user_active, user_id, False)
    if not ok:
        raise HTTPException(status_code=404, detail="API user not found")
    return {"deactivated": True, "user_id": user_id}


@router.patch("/users/{user_id}/activate")
async def activate_api_user(user_id: str):
    ok = await run_in_threadpool(bet_tracking.set_api_user_active, user_id, True)
    if not ok:
        raise HTTPException(status_code=404, detail="API user not found")
    return {"activated": True, "user_id": user_id}


@router.delete("/users/{user_id}")
async def delete_api_user(user_id: str):
    ok = await run_in_threadpool(bet_tracking.delete_api_user, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API user not found")
    return {"deleted": True, "user_id": user_id}
