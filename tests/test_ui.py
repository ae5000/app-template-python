# tests/test_ui.py

def test_ui_items_page_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert b"Items" in r.content

def test_ui_items_page_has_alpine_store(client):
    r = client.get("/")
    assert b"Alpine.store" in r.content or b"initAppStore" in r.content

def test_ui_item_detail_returns_html(client):
    created = client.post("/api/items", json={"name": "detail-test", "value": "v1"}).json()
    r = client.get(f"/items/{created['id']}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert b"detail-test" in r.content

def test_ui_item_detail_not_found(client):
    r = client.get("/items/nonexistent")
    assert r.status_code == 404

def test_ui_items_page_includes_user_json(client):
    r = client.get("/")
    # The SSR'd user object is embedded in a <script> tag
    assert b"dev@local" in r.content

def test_ui_items_page_has_config_block(client):
    r = client.get("/")
    assert b"window.__CONFIG__" in r.content
    assert b"apiBase" in r.content
