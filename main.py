from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from platform_sdk import init_platform, current_user, PlatformUser

app = FastAPI(title="my-service")
init_platform(app)

# ---------------------------------------------------------------------------
# In-memory store — replace with a real database
# ---------------------------------------------------------------------------

_items: dict[str, dict] = {}


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
# Routes
# ---------------------------------------------------------------------------

@app.get("/items", response_model=list[ItemResponse], operation_id="list_items",
         summary="List all items")
async def list_items(user: PlatformUser = Depends(current_user)):
    return [{"id": k, **v} for k, v in _items.items()]


@app.post("/items", response_model=ItemResponse, operation_id="create_item",
          summary="Create an item")
async def create_item(item: Item, user: PlatformUser = Depends(current_user)):
    import uuid
    item_id = str(uuid.uuid4())[:8]
    _items[item_id] = item.model_dump()
    return {"id": item_id, **_items[item_id]}


@app.get("/items/{item_id}", response_model=ItemResponse, operation_id="get_item",
         summary="Get an item by ID")
async def get_item(item_id: str, user: PlatformUser = Depends(current_user)):
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"id": item_id, **_items[item_id]}


@app.delete("/items/{item_id}", operation_id="delete_item",
            summary="Delete an item")
async def delete_item(item_id: str, user: PlatformUser = Depends(current_user)):
    user.require_group("engineering")  # layer-2 auth: restrict to engineering
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="Item not found")
    del _items[item_id]
    return {"deleted": item_id}
