
def test_create_channel_returns_id(client):
    r = client.post("/ws/channel")
    assert r.status_code == 200
    body = r.json()
    assert "channel_id" in body
    # uuid4 format: 8-4-4-4-12 hex chars
    assert len(body["channel_id"]) == 36

def test_ws_connect_valid_channel(client):
    channel_id = client.post("/ws/channel").json()["channel_id"]
    with client.websocket_connect(f"/ws/channel/{channel_id}") as ws:
        # Connection accepted — we can receive (nothing sent yet, so just verify open)
        pass  # context manager closes cleanly

def test_ws_connect_unknown_channel_closes(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises((WebSocketDisconnect, Exception)):
        with client.websocket_connect("/ws/channel/00000000-0000-0000-0000-000000000000") as ws:
            ws.receive_text()

def test_channel_receives_push_from_api(client):
    """Create a channel, open WS, call API with X-Channel-Id, verify patch arrives."""
    channel_id = client.post("/ws/channel").json()["channel_id"]
    with client.websocket_connect(f"/ws/channel/{channel_id}") as ws:
        # Create item via API with channel header
        client.post(
            "/api/items",
            json={"name": "ws-test", "value": "42"},
            headers={"X-Channel-Id": channel_id},
        )
        # Should receive an "add" patch on the WS
        patch = ws.receive_json()
        assert patch["op"] == "add"
        assert patch["path"] == "items"
        assert patch["value"]["name"] == "ws-test"
