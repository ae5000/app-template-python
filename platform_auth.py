"""
platform_auth.py — copy this file into any platform service.

Wiring in main.py:
    from contextlib import asynccontextmanager
    from platform_auth import PlatformAuthMiddleware, platform_lifespan, current_user, PlatformUser

    @asynccontextmanager
    async def lifespan(app):
        async with platform_lifespan():
            yield

    app = FastAPI(title="your-service", lifespan=lifespan)
    app.add_middleware(PlatformAuthMiddleware)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import yaml
from fastapi import HTTPException, Request
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

_SKIP_PATHS = {"/openapi.json", "/docs", "/redoc", "/health"}
_SKIP_PREFIX = ("/__platform/", "/static/")
_HEARTBEAT_INTERVAL = 60
_REGISTER_BACKOFF = [1, 2, 4, 8, 16]


# ---------------------------------------------------------------------------
# Secret reader — reads from Docker Swarm secrets, falls back to env var
# ---------------------------------------------------------------------------

def read_secret(name: str, default: str = "") -> str:
    """Read a secret value mounted by Docker Swarm, falling back to an env var.

    In production, secrets are mounted at /run/secrets/<name> by provision-service.sh.
    In local dev, the value is read from the environment variable <name> instead.

    Usage:
        db_url = read_secret("DATABASE_URL")
        api_key = read_secret("MY_API_KEY")
    """
    try:
        return open(f"/run/secrets/{name}").read().strip()  # noqa: SIM115
    except FileNotFoundError:
        return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

class PlatformUser(BaseModel):
    user_id: str = ""
    email: str | None = None
    groups: list[str] = []
    issued_at: int = 0
    expires_at: int = 0
    issued_by: str = ""

    def require_group(self, *groups: str) -> None:
        if not any(g in self.groups for g in groups):
            raise HTTPException(status_code=403, detail="Insufficient permissions")


# ---------------------------------------------------------------------------
# Auth middleware — trusts X-Platform-Auth injected by Traefik/auth-service
# ---------------------------------------------------------------------------

class PlatformAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in _SKIP_PATHS or any(path.startswith(p) for p in _SKIP_PREFIX):
            return await call_next(request)

        # Dev bypass: DEV_MOCK_USER=email:group1,group2
        mock = os.environ.get("DEV_MOCK_USER", "")
        if mock:
            email, _, groups_str = mock.partition(":")
            request.state.platform_user = PlatformUser(
                user_id="dev_user",
                email=email or "dev@local",
                groups=groups_str.split(",") if groups_str else ["admin", "engineering"],
                issued_at=0,
                expires_at=int(time.time()) + 86400,
                issued_by="mock",
            )
            return await call_next(request)

        auth_json = request.headers.get("X-Platform-Auth")
        if not auth_json:
            return JSONResponse(
                status_code=401,
                content={"error": "Missing X-Platform-Auth header"},
            )

        try:
            payload = json.loads(auth_json)
            if payload["expires_at"] < int(time.time()):
                return JSONResponse(
                    status_code=401,
                    content={"error": "Auth header expired"},
                )
            request.state.platform_user = PlatformUser(**payload)
        except Exception:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid X-Platform-Auth header"},
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def current_user(request: Request) -> PlatformUser:
    user = getattr(request.state, "platform_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ---------------------------------------------------------------------------
# Registration + heartbeat
# ---------------------------------------------------------------------------

def _load_config(path: str = "platform.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


async def _register_once(config: dict, openapi: dict | None = None) -> bool:
    svc = config["service"]
    rt = config.get("runtime", {})
    name = svc["name"]
    group = svc.get("group", "engineering")
    domain = _env("PLATFORM_DOMAIN")
    token = _env("PLATFORM_TOKEN")
    registry = _env("PLATFORM_REGISTRY_URL", "http://registry:8000")

    app_subdomain = name
    mcp_subdomain = f"{name}-mcp"
    payload = {
        "name": name,
        "groups": [group] if isinstance(group, str) else group,
        "description": svc.get("description", ""),
        "internal_url": f"http://{name}:{rt.get('port', 8000)}",
        "app_url": f"https://{app_subdomain}.{domain}",
        "mcp_url": f"https://{mcp_subdomain}.{domain}",
        "app_subdomain": app_subdomain,
        "mcp_subdomain": mcp_subdomain,
        "health_check": rt.get("health_check", "/health"),
        "platform_config": config,
        "openapi": openapi or {},
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{registry}/registry/register",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            log.info("registered with platform registry")
            return True
        log.warning("registry returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("registry registration failed: %s", exc)
    return False


async def _register_with_retry(config: dict, openapi: dict | None = None) -> None:
    for i, delay in enumerate(_REGISTER_BACKOFF):
        if await _register_once(config, openapi):
            return
        if i < len(_REGISTER_BACKOFF) - 1:
            log.info("retrying registration in %ds", delay)
            await asyncio.sleep(delay)
    log.error("platform registration failed after %d attempts", len(_REGISTER_BACKOFF))


async def _heartbeat_loop(name: str) -> None:
    registry = _env("PLATFORM_REGISTRY_URL", "http://registry:8000")
    token = _env("PLATFORM_TOKEN")
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{registry}/registry/{name}/heartbeat",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception as exc:
            log.warning("heartbeat failed: %s", exc)


@asynccontextmanager
async def platform_lifespan(app=None, config_path: str = "platform.yaml") -> AsyncGenerator:
    config = _load_config(config_path)
    name = config["service"]["name"]
    health_path = config.get("runtime", {}).get("health_check", "/health")

    if app is not None:
        from fastapi import FastAPI as _FastAPI
        from fastapi.routing import APIRoute

        existing = {r.path for r in app.routes if isinstance(r, APIRoute)}
        if health_path not in existing:
            @app.get(health_path, include_in_schema=False)
            async def _health():
                return {"status": "ok", "service": name}

    if not _env("DEV_MOCK_USER"):
        openapi = app.openapi() if app is not None else None
        await _register_with_retry(config, openapi)

    heartbeat = asyncio.create_task(_heartbeat_loop(name))
    yield
    heartbeat.cancel()
    try:
        await heartbeat
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# S2S client — forwards user context to internal services
# ---------------------------------------------------------------------------

class PlatformClient:
    """
    Async HTTP client for service-to-service calls.

    Production:  http://{service}:8000  (Docker internal DNS)
    Development: http://localhost:{SERVICE_NAME_PORT}

    Usage:
        client = PlatformClient("other-service")
        resp = await client.get("/items", user=user)
    """

    def __init__(self, target_service: str) -> None:
        self._service = target_service
        if _env("PLATFORM_ENV") == "development":
            port_key = target_service.upper().replace("-", "_") + "_PORT"
            port = _env(port_key)
            if not port:
                raise RuntimeError(
                    f"Set {port_key} for local S2S calls in development mode."
                )
            self._base_url = f"http://localhost:{port}"
        else:
            self._base_url = f"http://{target_service}:8000"

    def _headers(self, user: PlatformUser) -> dict[str, str]:
        return {
            "X-Platform-Auth": user.model_dump_json(),
            "X-Platform-Auth-Forwarded-By": _env("APP_NAME", self._service),
        }

    async def get(self, path: str, user: PlatformUser, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient() as c:
            return await c.get(self._base_url + path, headers=self._headers(user), **kwargs)

    async def post(self, path: str, user: PlatformUser, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient() as c:
            return await c.post(self._base_url + path, headers=self._headers(user), **kwargs)

    async def put(self, path: str, user: PlatformUser, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient() as c:
            return await c.put(self._base_url + path, headers=self._headers(user), **kwargs)

    async def patch(self, path: str, user: PlatformUser, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient() as c:
            return await c.patch(self._base_url + path, headers=self._headers(user), **kwargs)

    async def delete(self, path: str, user: PlatformUser, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient() as c:
            return await c.delete(self._base_url + path, headers=self._headers(user), **kwargs)
