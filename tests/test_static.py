# tests/test_static.py

def test_css_served(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]

def test_js_served(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]

def test_api_list_items_empty(client):
    r = client.get("/api/items")
    assert r.status_code == 200
    assert r.json() == []

def test_api_create_item(client):
    r = client.post("/api/items", json={"name": "foo", "value": "bar"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "foo"
    assert body["value"] == "bar"
    assert "id" in body

def test_api_get_item(client):
    created = client.post("/api/items", json={"name": "x", "value": "y"}).json()
    r = client.get(f"/api/items/{created['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "x"

def test_api_get_item_not_found(client):
    r = client.get("/api/items/nonexistent")
    assert r.status_code == 404

def test_api_delete_item(client):
    created = client.post("/api/items", json={"name": "del", "value": "me"}).json()
    r = client.delete(f"/api/items/{created['id']}")
    assert r.status_code == 200
    assert client.get(f"/api/items/{created['id']}").status_code == 404

def test_api_delete_requires_item_exists(client):
    # Delete non-existent item returns 404 (404 check happens before auth check)
    r = client.delete("/api/items/nonexistent-id")
    assert r.status_code == 404

def test_root_path_default(client):
    # FastAPI root_path is "" by default in tests
    r = client.get("/api/items")
    assert r.status_code == 200
