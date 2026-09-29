"""Google Sign-In for the desk.

Four people on two Google Workspace domains, so Google owns passwords, MFA and
offboarding and we keep only an allowlist. There is no password store here and
no reset flow to get wrong.

Two ways in, deliberately:

* People  — a signed session cookie, set after Google confirms the identity and
            the address is on ``ALLOWED_EMAILS``.
* Machines — Cloud Scheduler keeps hitting the delivery endpoints with
            ``X-Signals-Token``. Those paths bypass the session check and keep
            their own guard (``_require_token``), so turning sign-in on does not
            silently break the nine agent jobs.

When ``AUTH_ENABLED`` is false (either client credential missing) nothing is
enforced and a warning is logged once at startup — a deploy whose secrets are
not wired yet stays reachable instead of locking the desk out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from plgo_options.config import (
    ALLOWED_EMAILS,
    AUTH_ENABLED,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    SIGNALS_TOKEN,
)
from plgo_options.data.database import get_db

log = logging.getLogger(__name__)
router = APIRouter()

GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"

# Reachable without a session. Everything else needs one. /api/auth/ is here
# because the UI has to be able to ask "am I signed in?" while signed out —
# it reports session state and nothing about the book.
PUBLIC_PREFIXES = ("/login", "/auth/", "/api/auth/", "/static/", "/health", "/favicon.ico")

# Scheduler-driven endpoints. They carry X-Signals-Token and are guarded by
# their own routers; a browser session is not involved. Narrow on purpose:
# /api/agents/status and /api/agents/proposals are UI reads and stay protected.
MACHINE_PREFIXES = ("/api/agents/run/", "/api/signals/")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS app_users (
        email TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        picture TEXT DEFAULT '',
        first_seen TEXT NOT NULL,
        last_login TEXT NOT NULL,
        login_count INTEGER NOT NULL DEFAULT 0
    )""",
]

oauth = OAuth()
if AUTH_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url=GOOGLE_METADATA,
        client_kwargs={"scope": "openid email profile"},
    )


async def init_auth_tables() -> None:
    db = await get_db()
    for stmt in SCHEMA:
        await db.execute(stmt)
    await db.commit()


async def record_login(email: str, name: str, picture: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = await get_db()
    await db.execute(
        """INSERT INTO app_users (email, name, picture, first_seen, last_login, login_count)
           VALUES (?, ?, ?, ?, ?, 1)
           ON CONFLICT(email) DO UPDATE SET
             name=excluded.name, picture=excluded.picture,
             last_login=excluded.last_login, login_count=app_users.login_count + 1""",
        (email, name, picture, now, now),
    )
    await db.commit()


def is_allowed(email: str | None) -> bool:
    return bool(email) and email.strip().lower() in ALLOWED_EMAILS


def current_user(request: Request) -> dict[str, Any] | None:
    if not AUTH_ENABLED:
        return {"email": "auth-disabled", "name": "Auth disabled", "disabled": True}
    user = request.session.get("user")
    # The allowlist is re-checked on every request, not just at sign-in, so
    # removing an address takes effect immediately instead of at cookie expiry.
    return user if user and is_allowed(user.get("email")) else None


class AuthMiddleware(BaseHTTPMiddleware):
    """Require a signed-in, allowlisted user for everything except the
    public paths and the token-guarded scheduler endpoints."""

    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path
        if path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if path.startswith(MACHINE_PREFIXES):
            # Their own guard applies. Note it stays open while SIGNALS_TOKEN is
            # unset — that is the pre-existing fail-open in _require_token, not
            # something sign-in changes.
            if not SIGNALS_TOKEN or request.headers.get("X-Signals-Token") == SIGNALS_TOKEN:
                return await call_next(request)

        if current_user(request):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not signed in"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=302)
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    err = request.query_params.get("error", "")
    nxt = request.query_params.get("next", "/")
    note = (
        f'<p class="err">{err}</p>' if err else
        '<p class="sub">Desk access is limited to the four addresses on the allowlist.</p>'
    )
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<title>Sign in &middot; PLGO Options</title>
<style>
 body{{margin:0;height:100vh;display:grid;place-items:center;background:#0f172a;color:#e2e8f0;
      font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}
 .box{{background:#1e293b;padding:2.5rem 3rem;border-radius:12px;text-align:center;
       box-shadow:0 10px 40px rgba(0,0,0,.4);max-width:26rem}}
 h1{{margin:0 0 .25rem;font-size:1.25rem}}
 .sub{{color:#94a3b8;font-size:.85rem;margin:.25rem 0 1.5rem}}
 .err{{color:#fca5a5;font-size:.85rem;margin:.25rem 0 1.5rem}}
 a.btn{{display:inline-flex;align-items:center;gap:.6rem;background:#fff;color:#1f2937;
        text-decoration:none;padding:.65rem 1.25rem;border-radius:6px;font-weight:600;font-size:.9rem}}
 a.btn:hover{{background:#f1f5f9}}
</style></head><body><div class="box">
 <h1>PLGO Options</h1>{note}
 <a class="btn" href="/auth/login?next={nxt}">
   <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.6 30.1 0 24 0 14.6 0 6.4 5.4 2.6 13.2l7.8 6.1C12.2 13.2 17.6 9.5 24 9.5z"/><path fill="#4285F4" d="M46.1 24.6c0-1.6-.1-3.1-.4-4.6H24v9.1h12.4c-.5 2.9-2.1 5.3-4.6 7l7.6 5.9c4.4-4.1 6.7-10.1 6.7-17.4z"/><path fill="#FBBC05" d="M10.4 28.7c-.5-1.5-.8-3.1-.8-4.7s.3-3.2.8-4.7l-7.8-6.1C.9 16.3 0 20 0 24s.9 7.7 2.6 10.8l7.8-6.1z"/><path fill="#34A853" d="M24 48c6.1 0 11.3-2 15.1-5.5l-7.6-5.9c-2.1 1.4-4.8 2.2-7.5 2.2-6.4 0-11.8-3.7-13.6-9.1l-7.8 6.1C6.4 42.6 14.6 48 24 48z"/></svg>
   Sign in with Google</a>
</div></body></html>""")


@router.get("/auth/login")
async def auth_login(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=302)
    request.session["next"] = request.query_params.get("next", "/")
    return await oauth.google.authorize_redirect(request, str(request.url_for("auth_callback")))


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=302)
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as e:
        log.warning("Google sign-in failed: %s", e)
        return RedirectResponse(f"/login?error=Sign-in+failed:+{e.error}", status_code=302)

    info = token.get("userinfo") or {}
    email = (info.get("email") or "").strip().lower()

    if not info.get("email_verified", True):
        return RedirectResponse("/login?error=That+Google+address+is+not+verified.", status_code=302)
    if not is_allowed(email):
        log.warning("Rejected sign-in for %s (not on the allowlist)", email or "<no email>")
        return RedirectResponse(
            "/login?error=That+address+is+not+on+the+desk+allowlist.", status_code=302)

    request.session["user"] = {
        "email": email,
        "name": info.get("name") or email,
        "picture": info.get("picture") or "",
    }
    try:
        await record_login(email, info.get("name") or "", info.get("picture") or "")
    except Exception:  # a logging failure must not block sign-in
        log.exception("Could not record login for %s", email)

    nxt = request.session.pop("next", "/") or "/"
    return RedirectResponse(nxt if nxt.startswith("/") else "/", status_code=302)


@router.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@router.get("/api/auth/me")
async def auth_me(request: Request):
    u = current_user(request)
    return {"signed_in": bool(u), "auth_enabled": AUTH_ENABLED, "user": u}
