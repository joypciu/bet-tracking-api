"""
Authentication utilities and endpoints for Bet Tracking API.

Supports two auth methods:
  1. Cookie JWT (tracking_auth_token) -- browser / user auth
  2. Bearer token (BET_API_TOKEN)     -- service / script auth

Auth is ALWAYS required regardless of origin or hostname.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 60

_BET_API_TOKEN = os.getenv("BET_API_TOKEN", "").strip()
_bearer = HTTPBearer(auto_error=False)

if SECRET_KEY:
    print("[AUTH] JWT Secret Key loaded successfully")
else:
    print("[AUTH] WARNING: JWT_SECRET_KEY not set -- cookie auth disabled, Bearer token only")


# ---------------------------------------------------------------------------
# Auth dependency (used by all protected routes)
# ---------------------------------------------------------------------------

async def require_auth(
    request: Request,
    tracking_auth_token: str = Cookie(None),
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
):
    """
    Dual-mode auth dependency. Always enforced — no domain bypass.

    Priority:
      1. tracking_auth_token cookie (JWT) -- browser / user sessions
      2. Authorization: Bearer <BET_API_TOKEN> -- service / script access

    Returns:
      {"auth_type": "cookie", "email": ..., ...}  -- decoded JWT payload
      {"auth_type": "bearer", "email": None}       -- valid Bearer token
      Raises HTTP 401 if neither is present or valid.
    """
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")

    # --- 1. Try cookie JWT ---
    if tracking_auth_token and SECRET_KEY:
        try:
            payload = jwt.decode(
                tracking_auth_token, SECRET_KEY, algorithms=[ALGORITHM]
            )
            print("[AUTH] Cookie JWT: {} from {}".format(
                payload.get("email"), origin or host
            ))
            return {"auth_type": "cookie", **payload}
        except jwt.ExpiredSignatureError:
            print("[AUTH] Cookie JWT expired")
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            print("[AUTH] Cookie JWT invalid")
            raise HTTPException(status_code=401, detail="Invalid token")

    # --- 2. Try Bearer token ---
    if credentials and _BET_API_TOKEN and credentials.credentials == _BET_API_TOKEN:
        print("[AUTH] Bearer token from {}".format(origin or host))
        return {"auth_type": "bearer", "email": None}

    # --- 3. Nothing valid ---
    print("[AUTH] No valid auth from {}".format(origin or host))
    raise HTTPException(status_code=401, detail="Authentication required")


# ---------------------------------------------------------------------------
# Auth router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["auth"])


def _cookie_domain(origin: str) -> str | None:
    """Derive the correct cookie domain from the request origin."""
    if "127.0.0.1" in origin or "localhost" in origin:
        return None
    if "eternitylabs.co" in origin:
        return ".eternitylabs.co"
    return ".keepbetting.co"


def _me_url(origin: str) -> str:
    """Resolve the /me endpoint to call based on origin."""
    if "127.0.0.1" in origin or "localhost" in origin:
        return "http://127.0.0.1:8000/me"
    if "eternitylabs.co" in origin:
        return "https://bets-api.eternitylabs.co/me"
    return "https://app.keepbetting.co/api/me"


@router.get("/me")
async def get_current_user():
    """Dummy /me endpoint for local/dev — mimics app.keepbetting.co/api/me."""
    return {
        "id": 12232211234555,
        "name": "Bettor Doe",
        "email": "testaccount@eternitylabs.co",
        "avatar": "https://cdn.discordapp.com/avatars/142593177/None.jpg",
        "tier": ["ev", "all", "projections"],
        "created": 1766234460,
    }


@router.post("/auth/verify")
async def verify_and_issue_token(request: Request):
    """
    Verify the user is logged in (via app.keepbetting.co or local dummy),
    then issue a tracking_auth_token JWT cookie valid for TOKEN_EXPIRE_DAYS days.
    """
    if not SECRET_KEY:
        raise HTTPException(status_code=500, detail="JWT_SECRET_KEY not configured")

    origin = request.headers.get("origin", "")
    print(f"[AUTH] Verify request from origin: {origin}")

    is_allowed = (
        "localhost" in origin
        or "127.0.0.1" in origin
        or "keepbetting.co" in origin
        or "eternitylabs.co" in origin
    )
    if not is_allowed:
        raise HTTPException(
            status_code=403, detail="Authentication not applicable for this domain"
        )

    me_url = _me_url(origin)
    print(f"[AUTH] Checking user status at: {me_url}")

    async with httpx.AsyncClient() as client:
        try:
            auth_response = await client.get(
                me_url,
                cookies=request.cookies,
                timeout=10.0,
                follow_redirects=True,
            )
            print(f"[AUTH] Response status: {auth_response.status_code}")

            if auth_response.status_code != 200:
                raise HTTPException(
                    status_code=401, detail="Not authenticated on app.keepbetting.co"
                )

            user_data = auth_response.json()
            expiration_time = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
            token_payload = {
                "user_id": user_data.get("id"),
                "name": user_data.get("name"),
                "email": user_data.get("email"),
                "avatar": user_data.get("avatar"),
                "tier": user_data.get("tier", []),
                "created": user_data.get("created"),
                "exp": expiration_time,
                "iat": datetime.utcnow(),
            }
            token = jwt.encode(token_payload, SECRET_KEY, algorithm=ALGORITHM)

            is_local = "127.0.0.1" in origin or "localhost" in origin
            cookie_domain = _cookie_domain(origin)

            response = JSONResponse({
                "authenticated": True,
                "user": {
                    "user_id": user_data.get("id"),
                    "name": user_data.get("name"),
                    "email": user_data.get("email"),
                    "avatar": user_data.get("avatar"),
                    "tier": user_data.get("tier", []),
                },
            })
            cookie_kwargs: dict = {
                "key": "tracking_auth_token",
                "value": token,
                "max_age": TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
                "httponly": True,
                "secure": not is_local,
                "samesite": "lax",
            }
            if cookie_domain:
                cookie_kwargs["domain"] = cookie_domain
            response.set_cookie(**cookie_kwargs)
            return response

        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503, detail=f"Auth service unavailable: {str(exc)}"
            )


@router.get("/auth/check")
async def check_auth_token(tracking_auth_token: str = Cookie(None)):
    """Check whether the current tracking_auth_token cookie is valid."""
    if not SECRET_KEY:
        raise HTTPException(status_code=500, detail="JWT_SECRET_KEY not configured")
    if not tracking_auth_token:
        raise HTTPException(status_code=401, detail="No token provided")
    try:
        payload = jwt.decode(tracking_auth_token, SECRET_KEY, algorithms=[ALGORITHM])
        print(f"[AUTH] Token valid for: {payload.get('email')}")
        return {"authenticated": True, "user": payload}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


@router.post("/auth/logout")
async def logout(request: Request):
    """Clear the tracking_auth_token cookie."""
    origin = request.headers.get("origin", "")
    cookie_domain = _cookie_domain(origin)
    response = JSONResponse({"message": "Logged out"})
    if cookie_domain:
        response.delete_cookie("tracking_auth_token", domain=cookie_domain)
    else:
        response.delete_cookie("tracking_auth_token")
    return response
