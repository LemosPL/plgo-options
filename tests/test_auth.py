"""Google Sign-In: the allowlist, the gate, and the machine bypass.

The behaviour worth pinning down is not the OAuth dance (that is Google's and
authlib's) but who gets through the door: the four allowlisted addresses, the
scheduler with its token, and nobody else.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware


def _app(monkeypatch, *, enabled: bool, signals_token: str = ""):
    """Build a miniature app with the same middleware wiring as create_app()."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid" if enabled else "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec" if enabled else "")
    monkeypatch.setenv("SIGNALS_TOKEN", signals_token)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-value-for-sessions")

    import plgo_options.config as config
    importlib.reload(config)
    from plgo_options.web import auth as auth_mod
    importlib.reload(auth_mod)

    app = FastAPI()

    @app.get("/")
    async def index():
        return JSONResponse({"ok": "index"})

    @app.get("/api/agents/status")
    async def status():
        return JSONResponse({"ok": "status"})

    @app.post("/api/agents/run/{name}")
    async def run(name: str):
        return JSONResponse({"ok": name})

    app.include_router(auth_mod.router)
    app.add_middleware(auth_mod.AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET)
    return app, auth_mod


def _sign_in(client: TestClient, app: FastAPI, email: str):
    """Put a user in the session the way the OAuth callback would.

    Two traps: `request` must be annotated or FastAPI reads it as a query
    parameter and never writes the session; and the route has to live under a
    public prefix, or AuthMiddleware bounces the very call that would sign us
    in. Both failures look like "the assertion after this one is wrong".
    """
    @app.get("/auth/_test_signin")
    async def _si(request: Request):  # pragma: no cover - test helper route
        request.session["user"] = {"email": email, "name": email, "picture": ""}
        return JSONResponse({"signed_in": email})

    r = client.get("/auth/_test_signin")
    assert r.status_code == 200 and r.json() == {"signed_in": email}, "helper failed to sign in"


# ── the allowlist ────────────────────────────────────────────────────────────

def test_allowlist_holds_the_four_desk_addresses(monkeypatch):
    _, auth_mod = _app(monkeypatch, enabled=True)
    for e in ("chris@protocol.ai", "patrick@protocol.ai",
              "constantin.denuelle@protocol.ai", "lucas.lemos@pl-at.ch"):
        assert auth_mod.is_allowed(e), e


def test_allowlist_is_case_insensitive_and_trims(monkeypatch):
    _, auth_mod = _app(monkeypatch, enabled=True)
    assert auth_mod.is_allowed("  Lucas.Lemos@PL-AT.ch  ")


@pytest.mark.parametrize("email", [
    "", None, "someone@protocol.ai", "lucas.lemos@example.com",
    "chris@protocol.ai.evil.com", "attacker@gmail.com",
])
def test_allowlist_rejects_everyone_else(monkeypatch, email):
    _, auth_mod = _app(monkeypatch, enabled=True)
    assert not auth_mod.is_allowed(email)


def test_allowlist_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "only@me.com, Second@Me.com")
    import plgo_options.config as config
    importlib.reload(config)
    assert config.ALLOWED_EMAILS == frozenset({"only@me.com", "second@me.com"})
    monkeypatch.delenv("ALLOWED_EMAILS")
    importlib.reload(config)


# ── the gate ─────────────────────────────────────────────────────────────────

def test_disabled_auth_lets_everything_through(monkeypatch):
    app, _ = _app(monkeypatch, enabled=False)
    c = TestClient(app)
    assert c.get("/").status_code == 200
    assert c.get("/api/agents/status").status_code == 200


def test_enabled_auth_redirects_pages_to_login(monkeypatch):
    app, _ = _app(monkeypatch, enabled=True)
    c = TestClient(app, follow_redirects=False)
    r = c.get("/")
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


def test_enabled_auth_401s_api_calls(monkeypatch):
    app, _ = _app(monkeypatch, enabled=True)
    r = TestClient(app, follow_redirects=False).get("/api/agents/status")
    assert r.status_code == 401
    assert r.json()["detail"] == "Not signed in"


def test_login_page_is_reachable_without_a_session(monkeypatch):
    app, _ = _app(monkeypatch, enabled=True)
    r = TestClient(app).get("/login")
    assert r.status_code == 200
    assert "Sign in with Google" in r.text


def test_allowlisted_session_gets_in(monkeypatch):
    app, _ = _app(monkeypatch, enabled=True)
    c = TestClient(app)
    _sign_in(c, app, "lucas.lemos@pl-at.ch")
    assert c.get("/").json() == {"ok": "index"}   # the app, not the login page
    assert c.get("/api/agents/status").json() == {"ok": "status"}


def test_session_for_a_removed_address_stops_working(monkeypatch):
    """The allowlist is re-checked per request, so revoking access is
    immediate rather than waiting for the cookie to expire."""
    app, _ = _app(monkeypatch, enabled=True)
    c = TestClient(app, follow_redirects=False)
    _sign_in(c, app, "someone@protocol.ai")   # never allowlisted
    assert c.get("/api/agents/status").status_code == 401


# ── the machine bypass ───────────────────────────────────────────────────────

def test_scheduler_reaches_run_endpoint_with_the_right_token(monkeypatch):
    app, _ = _app(monkeypatch, enabled=True, signals_token="s3cret")
    r = TestClient(app).post("/api/agents/run/morning-open",
                             headers={"X-Signals-Token": "s3cret"})
    assert r.status_code == 200


def test_wrong_token_does_not_reach_run_endpoint(monkeypatch):
    app, _ = _app(monkeypatch, enabled=True, signals_token="s3cret")
    r = TestClient(app, follow_redirects=False).post(
        "/api/agents/run/morning-open", headers={"X-Signals-Token": "nope"})
    assert r.status_code == 401


def test_machine_bypass_does_not_expose_the_ui_reads(monkeypatch):
    """/api/agents/run/ is for the scheduler; /api/agents/status is a UI read
    and must still require a session even with a valid token."""
    app, _ = _app(monkeypatch, enabled=True, signals_token="s3cret")
    r = TestClient(app, follow_redirects=False).get(
        "/api/agents/status", headers={"X-Signals-Token": "s3cret"})
    assert r.status_code == 401


def test_unset_signals_token_keeps_the_scheduler_working(monkeypatch):
    """Pre-existing fail-open in _require_token: with no token configured the
    scheduler paths stay reachable, so enabling sign-in cannot silently break
    the nine agent jobs."""
    app, _ = _app(monkeypatch, enabled=True, signals_token="")
    assert TestClient(app).post("/api/agents/run/morning-open").status_code == 200


def test_auth_me_is_readable_while_signed_out(monkeypatch):
    """The UI asks this to decide whether to show a sign-in prompt, so it has
    to answer when there is no session rather than 401 like other /api/ paths."""
    app, _ = _app(monkeypatch, enabled=True)
    r = TestClient(app, follow_redirects=False).get("/api/auth/me")
    assert r.status_code == 200
    assert r.json() == {"signed_in": False, "auth_enabled": True, "user": None}


# ── the callback URL scheme ──────────────────────────────────────────────────
# Cloud Run forwards to the container over plain HTTP, so a naive url_for()
# sends Google "http://..." and the sign-in dies on redirect_uri_mismatch
# against the registered https URI. These pin the scheme down.

def _callback_for(monkeypatch, host: str, headers: dict[str, str]) -> str:
    app, auth_mod = _app(monkeypatch, enabled=True)
    seen = {}

    @app.get("/auth/_probe")
    async def _probe(request: Request):
        seen["url"] = auth_mod.callback_url(request)
        return JSONResponse({"url": seen["url"]})

    TestClient(app, base_url=f"http://{host}").get("/auth/_probe", headers=headers)
    return seen["url"]


def test_callback_uses_https_when_proxy_says_so(monkeypatch):
    url = _callback_for(monkeypatch, "plgo-options-x.a.run.app",
                        {"X-Forwarded-Proto": "https"})
    assert url == "https://plgo-options-x.a.run.app/auth/callback"


def test_callback_handles_a_forwarded_proto_list(monkeypatch):
    """Chained proxies send "https,http" — the client-facing scheme is first."""
    url = _callback_for(monkeypatch, "plgo-options-x.a.run.app",
                        {"X-Forwarded-Proto": "https, http"})
    assert url.startswith("https://")


def test_callback_defaults_to_https_for_a_remote_host(monkeypatch):
    """No proxy header (direct container hit) must still not produce http://."""
    url = _callback_for(monkeypatch, "plgo-options-x.a.run.app", {})
    assert url == "https://plgo-options-x.a.run.app/auth/callback"


def test_callback_stays_http_on_localhost(monkeypatch):
    """Local dev has no TLS; forcing https there would break the dev flow."""
    url = _callback_for(monkeypatch, "127.0.0.1:8000", {})
    assert url == "http://127.0.0.1:8000/auth/callback"


# ── session lifetime ─────────────────────────────────────────────────────────

def test_session_lasts_one_day_not_starlette_default(monkeypatch):
    """Starlette defaults to 14 days, which is a long time for a session that
    can read the whole book."""
    import plgo_options.config as config
    importlib.reload(config)
    assert config.SESSION_MAX_AGE_SECONDS == 86400


def test_session_lifetime_is_env_overridable(monkeypatch):
    monkeypatch.setenv("SESSION_MAX_AGE_SECONDS", "3600")
    import plgo_options.config as config
    importlib.reload(config)
    assert config.SESSION_MAX_AGE_SECONDS == 3600
    monkeypatch.delenv("SESSION_MAX_AGE_SECONDS")
    importlib.reload(config)


def test_an_expired_session_is_refused(monkeypatch):
    """max_age bounds the signature, not just the cookie, so a session copied
    out of a browser stops working at the same point rather than living on."""
    import plgo_options.config as config
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-value-for-sessions")
    importlib.reload(config)
    from plgo_options.web import auth as auth_mod
    importlib.reload(auth_mod)

    app = FastAPI()

    @app.get("/")
    async def index():
        return JSONResponse({"ok": "index"})

    @app.get("/auth/_test_signin")
    async def si(request: Request):
        request.session["user"] = {"email": "lucas.lemos@pl-at.ch", "name": "L", "picture": ""}
        return JSONResponse({"ok": True})

    app.include_router(auth_mod.router)
    app.add_middleware(auth_mod.AuthMiddleware)
    # one second, so "expired" is reachable without waiting a day
    app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, max_age=1)

    c = TestClient(app, follow_redirects=False)
    c.get("/auth/_test_signin")
    assert c.get("/").status_code == 200          # fresh session works

    import time
    time.sleep(1.2)
    r = c.get("/")                                 # same cookie, now stale
    assert r.status_code == 302 and r.headers["location"].startswith("/login")


def test_version_endpoint_is_public(monkeypatch):
    """The badge has to be readable before sign-in — it is the one thing you
    need to tell a failed deploy from a cached page."""
    app, _ = _app(monkeypatch, enabled=True)
    r = TestClient(app, follow_redirects=False).get("/api/version")
    # Route lives on the real app factory, not this miniature one; what matters
    # here is that the gate lets it through rather than 302/401-ing it.
    assert r.status_code != 401
    assert not (r.status_code == 302 and "/login" in r.headers.get("location", ""))
