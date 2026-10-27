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

from app.analyze import LABEL_PROMPT
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
        self.label = {"has_label": False, "colour": "other", "number": "", "confidence": "low", "note": ""}
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["system"] == LABEL_PROMPT:
            out = self.label
        else:
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
    assert r.json() == {"boxName": "Box Red 007", "exists": False, "unlabelled": False, "items": [], "photoCount": 0}

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


def test_read_label(setup):
    client, claude, _ = setup
    photo = [("photo", ("label.jpg", jpeg(), "image/jpeg"))]
    client.post("/api/save", data={"box_number": "12", "colour": "Blue", "items": '[{"name": "Kettle"}]'})

    claude.label = {"has_label": True, "colour": "Blue", "number": "12", "confidence": "high", "note": ""}
    r = client.post("/api/read-label", files=photo).json()
    assert r["label"]["number"] == "12" and r["label"]["colour"] == "Blue"
    assert r["box"]["boxName"] == "Box Blue 012" and r["box"]["exists"] and r["box"]["items"][0]["name"] == "Kettle"

    claude.label = {"has_label": True, "colour": "Red", "number": "12", "confidence": "medium", "note": ""}
    r = client.post("/api/read-label", files=photo).json()
    assert r["box"]["boxName"] == "Box Red 012" and r["box"]["exists"] is False

    # A colour that isn't configured: number kept, user picks the colour
    claude.label = {"has_label": True, "colour": "other", "number": "12", "confidence": "high", "note": ""}
    r = client.post("/api/read-label", files=photo).json()
    assert r["label"]["number"] == "12" and r["label"]["colour"] == "" and r["box"] is None

    claude.label = {"has_label": False, "colour": "other", "number": "", "confidence": "low", "note": "No label"}
    r = client.post("/api/read-label", files=photo).json()
    assert r["label"]["has_label"] is False and r["box"] is None and r["label"]["note"] == "No label"


def test_unlabelled_boxes(setup):
    client, claude, _ = setup
    assert client.get("/api/unlabelled").json() == {"boxes": []}

    # No number: each save makes the next unlabelled box
    r = client.post("/api/save", data={"items": '[{"name": "Tent"}, {"name": "Sleeping bags", "quantity": 2}]'},
                    files=[("photos", ("a.jpg", jpeg(), "image/jpeg"))]).json()
    assert r["boxName"] == "Unlabelled 001" and r["created"] and r["photosSaved"] == 1
    first = r["boxId"]
    r = client.post("/api/save", data={"items": '[{"name": "Paint"}]'}).json()
    assert r["boxName"] == "Unlabelled 002"
    second = r["boxId"]

    boxes = client.get("/api/unlabelled").json()["boxes"]
    assert [(b["boxName"], b["itemCount"]) for b in boxes] == [("Unlabelled 001", 2), ("Unlabelled 002", 1)]
    assert client.get(f"/api/box-by-id/{first}").json()["unlabelled"] is True

    # Adding more to an unlabelled box by id: Claude is told what's already there
    claude.items = [{"name": "Camping stove", "quantity": 1, "description": "", "tags": [], "source": "spoken",
                     "confidence": "high"}]
    client.post("/api/analyze", data={"box_id": first, "transcript": "and a stove"})
    assert "Tent x1" in claude.calls[-1]["messages"][0]["content"][-1]["text"]
    r = client.post("/api/save", data={"box_id": first, "items": '[{"name": "Camping stove"}]'}).json()
    assert r["boxName"] == "Unlabelled 001" and r["created"] is False

    # Label it later: it's renamed, keeps its contents, and leaves the unlabelled list
    r = client.post(f"/api/box-by-id/{first}/label", data={"box_number": "9", "colour": "blue"})
    assert r.status_code == 200, r.text
    assert r.json()["boxName"] == "Box Blue 009" and r.json()["unlabelled"] is False
    box = client.get("/api/box/9?colour=Blue").json()
    assert box["exists"] and box["id"] == first and len(box["items"]) == 3
    assert "Labelled Box Blue 009 (was Unlabelled 001)" in box["notes"]
    assert [b["boxName"] for b in client.get("/api/unlabelled").json()["boxes"]] == ["Unlabelled 002"]

    # Can't give a second box the same label
    r = client.post(f"/api/box-by-id/{second}/label", data={"box_number": "009", "colour": "Blue"})
    assert r.status_code == 409 and "Box Blue 009 already exists" in r.json()["detail"]

    # Numbering carries on after the highest unlabelled box
    assert client.post("/api/save", data={"items": "[]"}).json()["boxName"] == "Unlabelled 003"

    # Unknown ids are rejected
    assert client.get("/api/box-by-id/nope").status_code == 404
