import hmac

from fastapi import Header, HTTPException

from app.config import ADMIN_TOKEN, INTERNAL_TOKEN


def require_admin_token(x_admin_token: str | None = Header(default=None)):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN not configured")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="invalid admin token")


def require_internal_token(x_internal_token: str | None = Header(default=None)):
    if not INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="NENO_INTERNAL_TOKEN not configured")
    if not x_internal_token or not hmac.compare_digest(x_internal_token, INTERNAL_TOKEN):
        raise HTTPException(status_code=403, detail="invalid internal token")
