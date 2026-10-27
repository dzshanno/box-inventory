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

from app.analyze import LABEL_PROMPT, LOCATE_PROMPT
from app.search import SEARCH_PROMPT
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
        self.locate = {"boxes": [], "seen": []}
        self.search = lambda kwargs: {"matches": [], "note": "Nothing fits."}
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        system = kwargs["system"]
        if isinstance(system, list) and system[0]["text"] == SEARCH_PROMPT:
            out = self.search(kwargs)
        elif system == LABEL_PROMPT:
            out = self.label
        elif kwargs["system"] == LOCATE_PROMPT:
            out = self.locate
        else:
            out = {"box_summary": "Winter clothes", "items": self.items, "questions": ["Is the red coat yours?"]}
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(out))],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
        )


def jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (180, 120, 60)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def setup():
    root = f"Test Home {uuid.uuid4().hex[:6]}"  # keeps each test's places and boxes apart
    settings = replace(Settings(), homebox_url=URL, homebox_api_key=KEY, storage_root=root,
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


def test_search_and_edit(setup):
    client, _, _ = setup
    save = lambda colour, n, items: client.post("/api/save", data={
        "box_number": n, "colour": colour, "items": json.dumps(items)}).json()
    red = save("Red", "47", [{"name": "Christmas lights", "quantity": 3, "tags": ["Christmas"]},
                             {"name": "Tinsel", "description": "Gold and silver"}])
    blue = save("Blue", "47", [{"name": "Fairy lights", "description": "Battery, for the garden"}])
    save("Red", "3", [{"name": "Garden kneeler"}])

    # Items are grouped by box; matches in descriptions count too
    r = client.get("/api/search", params={"q": "lights"}).json()
    assert {g["boxName"]: [i["name"] for i in g["items"]] for g in r["results"]} == {
        "Box Red 047": ["Christmas lights"], "Box Blue 047": ["Fairy lights"]}
    r = client.get("/api/search", params={"q": "garden"}).json()
    assert sorted(g["boxName"] for g in r["results"]) == ["Box Blue 047", "Box Red 003"]
    # Box names match too
    r = client.get("/api/search", params={"q": "Blue 047"}).json()
    assert r["results"][0]["boxName"] == "Box Blue 047" and r["results"][0]["boxMatches"]
    assert client.get("/api/search", params={"q": "x"}).json()["results"] == []
    assert client.get("/api/search", params={"q": "snorkel"}).json()["results"] == []

    # Edit an item
    lights = client.get("/api/box/47?colour=Red").json()["items"]
    item = next(i for i in lights if i["name"] == "Christmas lights")
    r = client.put(f"/api/item/{item['id']}", data={"name": "Christmas lights (outdoor)", "quantity": "4",
                                                    "description": "Warm white", "tags": "Christmas, Outdoor"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Christmas lights (outdoor)" and r.json()["quantity"] == 4
    assert sorted(r.json()["tags"]) == ["Christmas", "Outdoor"]
    assert client.put(f"/api/item/{item['id']}", data={"name": "  "}).status_code == 400

    # Delete an item
    tinsel = next(i for i in lights if i["name"] == "Tinsel")
    assert client.delete(f"/api/item/{tinsel['id']}").status_code == 200
    assert [i["name"] for i in client.get("/api/box/47?colour=Red").json()["items"]] == ["Christmas lights (outdoor)"]
    assert client.delete(f"/api/item/{tinsel['id']}").status_code == 404

    # Boxes can't be edited or deleted as if they were items
    assert client.delete(f"/api/item/{red['boxId']}").status_code == 400
    # Labelled boxes can be relabelled too
    r = client.post(f"/api/box-by-id/{blue['boxId']}/label", data={"box_number": "48", "colour": "Blue"})
    assert r.json()["boxName"] == "Box Blue 048"


def test_locate(setup):
    client, claude, _ = setup
    red = client.post("/api/save", data={"box_number": "45", "colour": "Red", "items": "[]"}).json()
    blue = client.post("/api/save", data={"box_number": "3", "colour": "Blue", "items": "[]"}).json()
    photo = [("photo", ("room.jpg", jpeg(), "image/jpeg"))]  # 3000x2000, sent to Claude as 1568x1045

    # Several boxes in one photo; one found, one not
    claude.locate = {"boxes": [
        {"target": "red 45", "found": True, "x1": 784, "y1": 209, "x2": 1176, "y2": 627,
         "where": "centre stack, top", "confidence": "high"},
        {"target": "blue 3", "found": False, "x1": 0, "y1": 0, "x2": 0, "y2": 0, "where": "", "confidence": "low"},
    ], "seen": ["red 16", "red 45"]}
    r = client.post("/api/locate", data={"box_ids": f"{red['boxId']},{blue['boxId']}"}, files=photo)
    assert r.status_code == 200, r.text
    res = r.json()
    assert [(x["boxName"], x["found"]) for x in res["results"]] == [("Box Red 045", True), ("Box Blue 003", False)]
    assert res["results"][0]["box"] == [0.5, 0.2, 0.75, 0.6] and res["results"][0]["where"] == "centre stack, top"
    assert res["seen"] == ["red 16"]  # boxes we asked for aren't repeated
    prompt = claude.calls[-1]["messages"][0]["content"][1]["text"]
    assert "labelled: red 45, blue 3." in prompt

    # Claude leaving a box out counts as not found
    claude.locate = {"boxes": [], "seen": []}
    r = client.post("/api/locate", data={"box_ids": red["boxId"]}, files=photo).json()
    assert r["results"][0]["found"] is False and r["results"][0]["box"] is None

    # An unlabelled box has nothing to look for
    unl = client.post("/api/save", data={"items": "[]"}).json()
    r = client.post("/api/locate", data={"box_ids": unl["boxId"]}, files=photo)
    assert r.status_code == 400 and "without a label" in r.json()["detail"]


def test_claude_search(setup):
    client, claude, _ = setup
    a = client.post("/api/save", data={"box_number": "47", "colour": "Red", "items": json.dumps(
        [{"name": "Christmas lights", "quantity": 3}, {"name": "Cordless screwdriver", "description": "Bosch"}])}).json()
    client.post("/api/save", data={"box_number": "12", "colour": "Blue", "items": json.dumps(
        [{"name": "Fairy lights", "description": "garden"}])})

    def answer(kwargs):
        inventory = kwargs["system"][1]["text"]
        assert kwargs["system"][1]["cache_control"] == {"type": "ephemeral"}
        assert "My query" not in inventory  # the query goes in the message, not the cached part
        ids = {line.split(" | ")[2].split(" x")[0]: line.split(" | ")[0] for line in inventory.splitlines()[1:]}
        return {"matches": [
            {"id": ids["Fairy lights"], "why": "same thing", "confidence": "high"},
            {"id": ids["Christmas lights"], "why": "string lights", "confidence": "medium"},
            {"id": "i999", "why": "made up", "confidence": "high"},  # unknown ids are dropped
        ], "note": ""}
    claude.search = answer
    r = client.get("/api/search/claude", params={"q": "twinkly lights"})
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    assert [(g["boxName"], [i["name"] for i in g["items"]]) for g in res] == [
        ("Box Blue 012", ["Fairy lights"]), ("Box Red 047", ["Christmas lights"])]
    assert res[0]["items"][0]["why"] == "same thing"
    assert "What I'm looking for: twinkly lights" in claude.calls[-1]["messages"][0]["content"]

    # The quick search finds typos and half-typed words, across boxes
    r = client.get("/api/search", params={"q": "ligh"}).json()
    assert sorted(g["boxName"] for g in r["results"]) == ["Box Blue 012", "Box Red 047"]
    assert client.get("/api/search", params={"q": "screwdriver bosh"}).json()["results"][0]["boxId"] == a["boxId"]

    # The search list is refreshed after a change
    client.post("/api/save", data={"box_number": "47", "colour": "Red", "items": '[{"name": "Snorkel"}]'})
    assert client.get("/api/search", params={"q": "snorkel"}).json()["results"][0]["boxName"] == "Box Red 047"
    assert client.get("/api/search/claude", params={"q": " "}).status_code == 400


def test_places_and_last_seen(setup, monkeypatch):
    client, claude, _ = setup
    import app.main

    async def fake_areas(url, token):
        return ["Dressing Room", "Under the stairs", "Workshop"]
    monkeypatch.setattr(app.main, "area_names", fake_areas)

    # Nothing yet; Home Assistant areas are offered
    r = client.get("/api/places").json()
    assert r["places"] == [] and r["suggestions"] == ["Dressing Room", "Under the stairs", "Workshop"]

    workshop = client.post("/api/places", data={"name": " workshop "}).json()
    assert workshop["name"] == "workshop"
    assert client.post("/api/places", data={"name": "Workshop"}).json()["id"] == workshop["id"]  # no duplicates
    lockup = client.post("/api/places", data={"name": "Lockup"}).json()
    assert client.post("/api/places", data={"name": "Box Red 9"}).status_code == 400
    r = client.get("/api/places").json()
    assert [p["name"] for p in r["places"]] == ["Lockup", "workshop"]
    assert r["suggestions"] == ["Dressing Room", "Under the stairs"]  # Workshop is already a place

    # A new box goes where you say, and is seen there
    photo = [("photos", ("a.jpg", jpeg(), "image/jpeg"))]
    r = client.post("/api/save", data={"box_number": "32", "colour": "Red", "location_id": workshop["id"],
                                        "items": '[{"name": "Baubles"}]'}, files=photo).json()
    assert r["created"] and r["place"] == "workshop" and r["movedFrom"] is None
    box = client.get("/api/box/32?colour=Red").json()
    assert box["place"]["name"] == "workshop"
    assert box["lastSeen"]["place"] == "workshop" and box["lastSeen"]["how"] == "photographed"
    assert len(box["lastSeen"]["at"]) == 16  # "2026-10-27 13:20"

    # Labels are unique everywhere: adding to Red 32 from the lockup moves it there
    r = client.post("/api/save", data={"box_number": "32", "colour": "Red", "location_id": lockup["id"],
                                        "items": '[{"name": "Tinsel"}]'}).json()
    assert r["created"] is False and r["movedFrom"] == "workshop" and r["place"] == "Lockup"
    box = client.get("/api/box/32?colour=Red").json()
    assert box["place"]["name"] == "Lockup" and len(box["items"]) == 2
    assert "Moved from workshop" in box["notes"] and box["lastSeen"]["how"] == "updated"

    # Moving from the box page
    r = client.post(f"/api/box-by-id/{box['id']}/move", data={"location_id": workshop["id"]}).json()
    assert r["place"]["name"] == "workshop" and r["lastSeen"]["how"] == "moved here"
    assert client.post(f"/api/box-by-id/{box['id']}/move", data={"location_id": "nope"}).status_code == 400

    # No place given: the default place
    r = client.post("/api/save", data={"box_number": "5", "colour": "Blue", "items": "[]"}).json()
    assert r["place"] == "Storage Unit"

    # Searches show where each box is and when it was last seen; place names are searchable
    g = client.get("/api/search", params={"q": "baubles"}).json()["results"][0]
    assert g["boxName"] == "Box Red 032" and g["place"] == "workshop" and g["lastSeen"]["how"] == "moved here"
    assert [g["boxName"] for g in client.get("/api/search", params={"q": "workshop"}).json()["results"]] == ["Box Red 032"]

    # Spotting a box in a photo records the spot
    claude.locate = {"boxes": [{"target": "red 32", "found": True, "x1": 10, "y1": 10, "x2": 200, "y2": 200,
                                "where": "top shelf, left", "confidence": "high"}], "seen": []}
    client.post("/api/locate", data={"box_ids": box["id"]}, files=[("photo", ("r.jpg", jpeg(), "image/jpeg"))])
    seen = client.get(f"/api/box-by-id/{box['id']}").json()["lastSeen"]
    assert seen["how"] == "spotted in a photo" and seen["spot"] == "top shelf, left" and seen["place"] == "workshop"
