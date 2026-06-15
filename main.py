import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import (
    FastAPI, Depends, HTTPException, Header, Request,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from db import DB, init_db
from platform_auth import PlatformAuthMiddleware, platform_lifespan, current_user, PlatformUser

_REGISTRY_URL = os.getenv("PLATFORM_REGISTRY_URL", "http://registry:8000")

# ---------------------------------------------------------------------------
# Database — initialised in lifespan, available to all routes
# ---------------------------------------------------------------------------

db: DB = DB()  # not connected until lifespan runs


@asynccontextmanager
async def lifespan(app):
    global db
    db = await init_db()
    await db.run_migrations("sql")
    # Inject db status into all Jinja2 templates as a global
    templates.env.globals["db_connected"] = db.connected
    templates.env.globals["db_backend"] = db.backend
    async with platform_lifespan(app):
        yield
    await db.close()


ROOT_PATH = os.getenv("ROOT_PATH", "")
app = FastAPI(title="my-service", root_path=ROOT_PATH, lifespan=lifespan)
app.add_middleware(PlatformAuthMiddleware)


def _read_debug_flag() -> bool:
    try:
        path = os.path.expanduser("~/.bgrx-agents/debug.yaml")
        with open(path) as f:
            for line in f:
                if line.strip().startswith("show_state:"):
                    return line.split(":", 1)[1].strip().lower() in ("true", "yes", "1")
    except Exception:
        pass
    return False


_DEBUG_SHOW_STATE = _read_debug_flag()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["db_connected"] = False   # overwritten in lifespan
templates.env.globals["db_backend"] = "none"

_STATIC_VERSION = str(int(os.path.getmtime("static/app.js")))

# ---------------------------------------------------------------------------
# In-memory stores (channels + jobs are ephemeral by nature — no DB needed)
# ---------------------------------------------------------------------------

_channels: dict[str, dict] = {}  # channel_id -> {"user_id": str, "ws": WebSocket | None}
_jobs: dict[str, dict] = {}      # job_id -> {"status": str, "progress": int, "log": list}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Item(BaseModel):
    name: str
    value: str


class ItemResponse(BaseModel):
    id: str
    name: str
    value: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def push(channel_id: str | None, patch: dict) -> None:
    """Push a WS patch to a channel. No-op if channel_id is None or unknown."""
    if not channel_id or channel_id not in _channels:
        return
    ws = _channels[channel_id].get("ws")
    if ws is not None:
        try:
            await ws.send_json(patch)
        except Exception:
            pass


def _user_ctx(user: PlatformUser) -> dict:
    initials = (user.email or "?")[:2].upper()
    return {
        "id": user.user_id,
        "name": user.email,
        "initials": initials,
        "email": user.email,
    }


# ---------------------------------------------------------------------------
# Health — override platform_lifespan's default so we can include DB status
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
async def health():
    return {
        "status": "ok" if db.connected else "degraded",
        "db": db.backend,
    }


# ---------------------------------------------------------------------------
# Platform proxy routes
# ---------------------------------------------------------------------------

@app.get("/__platform/nav", include_in_schema=False)
async def platform_nav(request: Request, user: PlatformUser = Depends(current_user)):
    auth = request.headers.get("X-Platform-Auth", "{}")
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(
            f"{_REGISTRY_URL}/portal/nav-services",
            headers={"X-Platform-Auth": auth},
        )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/__platform/branding", include_in_schema=False)
async def platform_branding():
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{_REGISTRY_URL}/portal/branding")
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


# ---------------------------------------------------------------------------
# WebSocket channel routes
# ---------------------------------------------------------------------------

@app.post("/ws/channel", include_in_schema=False)
async def create_channel(user: PlatformUser = Depends(current_user)):
    channel_id = str(uuid.uuid4())
    _channels[channel_id] = {"user_id": user.user_id, "ws": None}
    return {"channel_id": channel_id}


@app.websocket("/ws/channel/{channel_id}")
async def channel_ws(websocket: WebSocket, channel_id: str):
    if channel_id not in _channels:
        await websocket.close(code=4004)
        return
    await websocket.accept()
    _channels[channel_id]["ws"] = websocket
    try:
        while True:
            await websocket.receive_text()  # keep-alive; client sends nothing
    except WebSocketDisconnect:
        pass
    finally:
        _channels.pop(channel_id, None)


# ---------------------------------------------------------------------------
# UI routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui_items(request: Request, user: PlatformUser = Depends(current_user)):
    items = await db.fetch("SELECT id, name, value FROM items ORDER BY created_at")
    return templates.TemplateResponse(request, "pages/items.html", {
        "user": _user_ctx(user),
        "items": items,
        "static_version": _STATIC_VERSION,
        "debug_show_state": _DEBUG_SHOW_STATE,
    })


@app.get("/items/{item_id}", response_class=HTMLResponse, include_in_schema=False)
async def ui_item_detail(
    request: Request, item_id: str, user: PlatformUser = Depends(current_user)
):
    item = await db.fetchrow("SELECT id, name, value FROM items WHERE id=$1", item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return templates.TemplateResponse(request, "pages/item_detail.html", {
        "user": _user_ctx(user),
        "item": item,
        "static_version": _STATIC_VERSION,
        "debug_show_state": _DEBUG_SHOW_STATE,
    })


# ---------------------------------------------------------------------------
# API routes  (JSON — also surfaced to CLI + MCP via x-platform annotation)
# ---------------------------------------------------------------------------
# openapi_extra={"x-platform": {"cli": {"command": "<service> <subcommand>", "args": [...]}}}
#
# "command" → bgrx <service> <subcommand>  (first word = service group)
# "args"    → list of {name, type, required, choices, positional}
# "mcp"     → auto-derived from cli.command (tool_name = command with spaces→underscores)
#             set "mcp": False to suppress MCP exposure for a route
# ---------------------------------------------------------------------------

@app.get(
    "/api/items",
    response_model=list[ItemResponse],
    operation_id="list_items",
    summary="List all items",
    openapi_extra={"x-platform": {"cli": {"command": "my-service list-items"}}},
)
async def list_items(user: PlatformUser = Depends(current_user)):
    return await db.fetch("SELECT id, name, value FROM items ORDER BY created_at")


@app.post(
    "/api/items",
    response_model=ItemResponse,
    operation_id="create_item",
    summary="Create an item",
    openapi_extra={"x-platform": {"cli": {"command": "my-service create-item", "args": [
        {"name": "name", "type": "string", "required": True},
        {"name": "value", "type": "string", "required": True},
    ]}}},
)
async def create_item(
    item: Item,
    user: PlatformUser = Depends(current_user),
    channel_id: str | None = Header(None, alias="X-Channel-Id"),
):
    item_id = str(uuid.uuid4())[:8]
    await db.execute(
        "INSERT INTO items (id, name, value) VALUES ($1, $2, $3)",
        item_id, item.name, item.value,
    )
    result = {"id": item_id, "name": item.name, "value": item.value}
    await push(channel_id, {"op": "add", "path": "items", "value": result})
    return result


@app.get(
    "/api/items/{item_id}",
    response_model=ItemResponse,
    operation_id="get_item",
    summary="Get an item by ID",
)
async def get_item(item_id: str, user: PlatformUser = Depends(current_user)):
    row = await db.fetchrow("SELECT id, name, value FROM items WHERE id=$1", item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    return row


@app.delete(
    "/api/items/{item_id}",
    operation_id="delete_item",
    summary="Delete an item",
)
async def delete_item(
    item_id: str,
    user: PlatformUser = Depends(current_user),
    channel_id: str | None = Header(None, alias="X-Channel-Id"),
):
    user.require_group("engineering")
    result = await db.execute("DELETE FROM items WHERE id=$1", item_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Item not found")
    await push(channel_id, {"op": "remove", "path": "items", "id": item_id})
    return {"deleted": item_id}


# ---------------------------------------------------------------------------
# Jobs — long-running task demo
# ---------------------------------------------------------------------------

_JOB_STEPS = 36          # 5s × 36 = 180s total
_JOB_STEP_SECS = 5
_JOB_BATCH_SIZE = 500    # records per step (cosmetic)

_JOB_PREAMBLE = [
    "Connecting to source database",
    "Validating schema and permissions",
    "Counting source records: found 18,000",
    "Allocating output buffer",
]


async def _run_job(job_id: str, channel_id: str | None) -> None:
    for msg in _JOB_PREAMBLE:
        await asyncio.sleep(1)
        _jobs[job_id]["log"].append(msg)
        await push(channel_id, {"op": "append-log", "path": "job.log", "value": msg})

    for step in range(1, _JOB_STEPS + 1):
        await asyncio.sleep(_JOB_STEP_SECS)
        progress = round(step * 100 / _JOB_STEPS)
        lo = (step - 1) * _JOB_BATCH_SIZE + 1
        hi = step * _JOB_BATCH_SIZE
        msg = f"[{step:02d}/{_JOB_STEPS}] Exported records {lo:,}–{hi:,}"
        _jobs[job_id]["progress"] = progress
        _jobs[job_id]["log"].append(msg)
        await push(channel_id, {"op": "set",        "path": "job.progress", "value": progress})
        await push(channel_id, {"op": "append-log", "path": "job.log",      "value": msg})

    _jobs[job_id]["status"] = "done"
    await push(channel_id, {"op": "append-log", "path": "job.log",    "value": "Export complete — 18,000 records written"})
    await push(channel_id, {"op": "set",        "path": "job.status", "value": "done"})


@app.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
async def ui_jobs(request: Request, user: PlatformUser = Depends(current_user)):
    return templates.TemplateResponse(request, "pages/jobs.html", {
        "user": _user_ctx(user),
        "static_version": _STATIC_VERSION,
        "debug_show_state": _DEBUG_SHOW_STATE,
    })


@app.post(
    "/api/jobs",
    operation_id="create_job",
    summary="Start a long-running background job (180s)",
)
async def create_job(
    user: PlatformUser = Depends(current_user),
    channel_id: str | None = Header(None, alias="X-Channel-Id"),
):
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "progress": 0, "log": []}
    asyncio.create_task(_run_job(job_id, channel_id))
    return {"job_id": job_id}
