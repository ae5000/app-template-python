import os
import uuid

from fastapi import (
    FastAPI, Depends, HTTPException, Header, Request,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from platform_sdk import init_platform, current_user, PlatformUser

ROOT_PATH = os.getenv("ROOT_PATH", "")
app = FastAPI(title="my-service", root_path=ROOT_PATH)
init_platform(app)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

_items: dict[str, dict] = {}
_channels: dict[str, dict] = {}  # channel_id -> {"user_id": str, "ws": WebSocket | None}


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
        _channels.pop(channel_id, None)


# ---------------------------------------------------------------------------
# UI routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui_items(request: Request, user: PlatformUser = Depends(current_user)):
    items = [{"id": k, **v} for k, v in _items.items()]
    return templates.TemplateResponse("pages/items.html", {
        "request": request,
        "user": _user_ctx(user),
        "items": items,
    })


@app.get("/items/{item_id}", response_class=HTMLResponse, include_in_schema=False)
async def ui_item_detail(
    request: Request, item_id: str, user: PlatformUser = Depends(current_user)
):
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="Item not found")
    return templates.TemplateResponse("pages/item_detail.html", {
        "request": request,
        "user": _user_ctx(user),
        "item": {"id": item_id, **_items[item_id]},
    })


# ---------------------------------------------------------------------------
# API routes  (JSON — also used by CLI / MCP)
# ---------------------------------------------------------------------------

@app.get(
    "/api/items",
    response_model=list[ItemResponse],
    operation_id="list_items",
    summary="List all items",
)
async def list_items(user: PlatformUser = Depends(current_user)):
    return [{"id": k, **v} for k, v in _items.items()]


@app.post(
    "/api/items",
    response_model=ItemResponse,
    operation_id="create_item",
    summary="Create an item",
)
async def create_item(
    item: Item,
    user: PlatformUser = Depends(current_user),
    channel_id: str | None = Header(None, alias="X-Channel-Id"),
):
    item_id = str(uuid.uuid4())[:8]
    _items[item_id] = item.model_dump()
    result = {"id": item_id, **_items[item_id]}
    await push(channel_id, {"op": "add", "path": "items", "value": result})
    return result


@app.get(
    "/api/items/{item_id}",
    response_model=ItemResponse,
    operation_id="get_item",
    summary="Get an item by ID",
)
async def get_item(item_id: str, user: PlatformUser = Depends(current_user)):
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"id": item_id, **_items[item_id]}


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
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="Item not found")
    user.require_group("engineering")
    del _items[item_id]
    await push(channel_id, {"op": "remove", "path": "items", "id": item_id})
    return {"deleted": item_id}
