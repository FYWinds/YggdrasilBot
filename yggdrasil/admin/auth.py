"""Dashboard authentication.

oauth2-proxy (Traefik forwardAuth) authenticates the user and forwards the email in
``APP_CONFIG.auth_email_header``; we only check it against the admin whitelist. In dev
the header may be absent and ``YGG_DEV_ADMIN_EMAIL`` is trusted instead.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from yggdrasil import runtime
from yggdrasil.config import APP_CONFIG


def _dev_email() -> str | None:
    if APP_CONFIG.environment == "dev" and APP_CONFIG.dev_admin_email:
        return APP_CONFIG.dev_admin_email.strip().lower()
    return None


async def require_admin(request: Request) -> str:
    """Return the acting admin's email (used as ``config_audit.actor``)."""
    email = (request.headers.get(APP_CONFIG.auth_email_header) or "").strip().lower()
    dev = _dev_email()
    if not email:
        if dev is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
        return dev
    if email == dev or email in runtime.snapshot().admin_emails:
        return email
    raise HTTPException(status.HTTP_403_FORBIDDEN, f"{email} 不是管理员")


Actor = Annotated[str, Depends(require_admin)]
