"""Tests that run without a Homebox: box naming, bad input, and Homebox being unreachable."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.homebox import Homebox
from app.main import create_app


def unreachable(request):
    raise httpx.ConnectError("connection refused", request=request)


def client_for(handler):
    settings = replace(Settings(), homebox_url="http://hb.invalid", homebox_api_key="hb_x")
    hb = Homebox("http://hb.invalid", "hb_x", transport=httpx.MockTransport(handler))
    return TestClient(create_app(settings, homebox=hb, claude=SimpleNamespace()))


def test_box_name_ignores_extra_leading_zeros():
    s = replace(Settings(), box_name_format="Box {n}", box_number_digits=3)
    assert s.box_name("42") == s.box_name("042") == s.box_name("0042") == "Box 042"
    assert s.box_name("1234") == "Box 1234"
    assert s.box_name("A1") == "Box A1"


def test_homebox_unreachable_gives_clear_error():
    with client_for(unreachable) as client:
        r = client.get("/api/box/1")
    assert r.status_code == 502
    assert "Couldn't reach Homebox at http://hb.invalid" in r.json()["detail"]


def test_homebox_unreachable_on_login():
    hb_settings = replace(Settings(), homebox_url="http://hb.invalid", homebox_api_key="")
    hb = Homebox("http://hb.invalid", username="u", password="p", transport=httpx.MockTransport(unreachable))
    with TestClient(create_app(hb_settings, homebox=hb, claude=SimpleNamespace())) as client:
        r = client.get("/api/box/1")
    assert r.status_code == 502
    assert "Couldn't reach Homebox" in r.json()["detail"]


def test_save_rejects_items_that_are_not_objects():
    with client_for(unreachable) as client:
        for bad in ('["boots"]', '[1, 2]', '[{"name": "ok"}, null]', '{}', 'nope'):
            r = client.post("/api/save", data={"box_number": "1", "items": bad})
            assert r.status_code == 400, bad
            assert "JSON list" in r.json()["detail"]
