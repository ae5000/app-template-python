import os
os.environ["SKIP_PLATFORM_AUTH"] = "true"

import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    from main import app
    with TestClient(app) as c:
        yield c

@pytest.fixture(autouse=True)
def reset_items():
    from main import _items, _channels
    _items.clear()
    _channels.clear()
    yield
