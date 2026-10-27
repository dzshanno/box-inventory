"""End-to-end test against a real Homebox, with Claude replaced by a stand-in.

Run with a disposable Homebox:
    HOMEBOX_TEST_URL=http://localhost:7745 HOMEBOX_TEST_API_KEY=hb_... pytest
"""

import io
import json
import os
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.homebox import Homebox
from app.main import create_app

URL = os.environ.get("HOMEBOX_TEST_URL")
KEY = os.environ.get("HOMEBOX_TEST_API_KEY")
pytestmark = pytest.mark.skipif(not (URL and KEY), reason="needs HOMEBOX_TEST_URL and HOMEBOX_TEST_API_KEY")


class FakeClaude:
    """Mimics client.beta.messages.create and records what it was sent."""

    def __init__(self, items):
        self.items = items
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        out = {"box_summary": "Winter clothes", "items": self.items, "questions": ["Is the red coat yours?"]}
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(out))],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (180, 120, 60)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def setup():
    parent = f"Test Unit {uuid.uuid4().hex[:6]}"
    settings = replace(Settings(), homebox_url=URL, homebox_api_key=KEY, parent_location=parent,
                       homebox_public_url="http://hb.example")
    claude = FakeClaude([
        {"name": "Winter boots", "quantity": 2, "description": "Black leather", "tags": ["Clothing", "Shoes"],
         "source": "both", "confidence": "high"},
        {"name": "Scarves", "quantity": 4, "description": "Wool", "tags": ["clothing"],
         "source": "spoken", "confidence": "medium"},
    ])
    app = create_app(settings, homebox=Homebox(URL, KEY), claude=claude)
    with TestClient(app) as client:
        yield client, claude, settings


def test_full_flow(setup):
    client, claude, settings = setup

    # A new box doesn't exist yet
    r = client.get("/api/box/7?colour=red")
    assert r.status_code == 200
    assert r.json() == {"boxName": "Box Red 007", "exists": False, "items": [], "photoCount": 0}

    # Analyse: photos are shrunk for Claude, transcript passed through
    r = client.post("/api/analyze", data={"box_number": "7", "colour": "Red",
                                           "transcript": "two pairs of boots and some scarves"},
                    files=[("photos", ("a.jpg", jpeg(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    draft = r.json()
    assert [i["name"] for i in draft["items"]] == ["Winter boots", "Scarves"]
    call = claude.calls[-1]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default"
    content = call["messages"][0]["content"]
    assert any(b["type"] == "image" for b in content)
    assert "two pairs of boots" in content[-1]["text"]

    # Save (user edited one item)
    items = draft["items"]
    items[1]["quantity"] = 5
    r = client.post("/api/save", data={"box_number": "7", "colour": "Red", "items": json.dumps(items),
                                        "transcript": "two pairs of boots and some scarves",
                                        "box_summary": draft["box_summary"]},
                    files=[("photos", ("a.jpg", jpeg(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["created"] is True and res["itemsSaved"] == 2 and res["photosSaved"] == 1
    assert res["link"] == f"http://hb.example/location/{res['boxId']}"

    # Box now exists with the items, photo and notes
    box = client.get("/api/box/007?colour=Red").json()
    assert box["exists"] and box["photoCount"] == 1
    got = {i["name"]: i for i in box["items"]}
    assert got["Scarves"]["quantity"] == 5
    assert sorted(got["Winter boots"]["tags"]) == ["Clothing", "Shoes"]
    assert got["Scarves"]["tags"] == ["Clothing"]  # "clothing" matched the existing tag
    assert "Spoken description: two pairs of boots" in box["notes"]

    # Coming back later: Claude is told what's already there, and new things are added
    claude.items = [{"name": "Gloves", "quantity": 3, "description": "", "tags": ["Clothing"],
                     "source": "spoken", "confidence": "high"}]
    r = client.post("/api/analyze", data={"box_number": "7", "colour": "Red", "transcript": "also gloves"})
    assert r.status_code == 200
    assert "Already recorded in this box" in claude.calls[-1]["messages"][0]["content"][-1]["text"]
    assert "Winter boots x2" in claude.calls[-1]["messages"][0]["content"][-1]["text"]
    r = client.post("/api/save", data={"box_number": "7", "colour": "Red", "items": json.dumps(r.json()["items"]),
                                        "transcript": "also gloves"})
    assert r.json()["created"] is False
    box = client.get("/api/box/7?colour=Red").json()
    assert len(box["items"]) == 3
    assert box["notes"].count("Spoken description") == 2

    # Blue 007 is a different box: still empty, and saving to it leaves Red 007 alone
    assert client.get("/api/box/7?colour=Blue").json()["exists"] is False
    r = client.post("/api/save", data={"box_number": "7", "colour": "Blue",
                                        "items": json.dumps([{"name": "Paint tins", "quantity": 3}])})
    assert r.json()["created"] is True and r.json()["boxName"] == "Box Blue 007"
    assert [i["name"] for i in client.get("/api/box/7?colour=Blue").json()["items"]] == ["Paint tins"]
    assert len(client.get("/api/box/7?colour=Red").json()["items"]) == 3


def test_password_gate(setup):
    _, claude, settings = setup
    app = create_app(replace(settings, app_password="s3cret"), homebox=Homebox(URL, KEY), claude=claude)
    with TestClient(app) as client:
        assert client.get("/api/config").status_code == 401
        assert client.get("/api/config", auth=("any", "wrong")).status_code == 401
        assert client.get("/api/config", auth=("any", "s3cret")).status_code == 200
        assert client.get("/healthz").status_code == 200


def test_needs_photo_or_description(setup):
    client, _, _ = setup
    r = client.post("/api/analyze", data={"box_number": "8", "colour": "Blue", "transcript": "  "})
    assert r.status_code == 422
    assert "photo or a description" in r.json()["detail"]
