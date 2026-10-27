"""Tests that run without a Homebox: box naming, bad input, and Homebox being unreachable."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, _colours
from app.homebox import Homebox
from app.main import create_app


def unreachable(request):
    raise httpx.ConnectError("connection refused", request=request)


def client_for(handler):
    settings = replace(Settings(), homebox_url="http://hb.invalid", homebox_api_key="hb_x")
    hb = Homebox("http://hb.invalid", "hb_x", transport=httpx.MockTransport(handler))
    return TestClient(create_app(settings, homebox=hb, claude=SimpleNamespace()))


def test_box_name_ignores_extra_leading_zeros():
    s = replace(Settings(), box_number_digits=3)
    assert s.box_name("42", "Red") == s.box_name("042", "red") == s.box_name("0042", " RED ") == "Box Red 042"
    assert s.box_name("1234", "Blue") == "Box Blue 1234"
    assert s.box_name("A1", "Blue") == "Box Blue A1"


def test_colour_is_part_of_the_box():
    s = Settings()
    assert s.box_name("42", "Red") != s.box_name("42", "Blue")
    with pytest.raises(ValueError, match="Pick a label colour"):
        s.box_name("42")
    with pytest.raises(ValueError, match="Unknown label colour 'Green'"):
        s.box_name("42", "Green")


def test_colours_setting():
    s = replace(Settings(), box_colours=_colours("Red, Blue, Green=#2e7d32,"))
    assert s.box_colours == (("Red", "red"), ("Blue", "blue"), ("Green", "#2e7d32"))
    assert s.box_name("1", "green") == "Box Green 001"


def test_no_colours_configured():
    s = replace(Settings(), box_colours=())
    assert s.box_name("42") == s.box_name("42", "anything") == "Box 042"
    s = replace(Settings(), box_colours=(), box_name_format="Box {n}")
    assert s.box_name("42") == "Box 042"


def test_name_format_must_include_colour_when_several():
    with pytest.raises(ValueError, match="must include {colour}"):
        replace(Settings(), box_name_format="Box {n}")
    assert replace(Settings(), box_colours=(("Red", "red"),), box_name_format="Box {n}").box_name("4", "Red") == "Box 004"


def test_unknown_colour_rejected_by_api():
    with client_for(unreachable) as client:
        r = client.get("/api/box/1?colour=green")
        assert r.status_code == 400 and "Unknown label colour" in r.json()["detail"]
        r = client.get("/api/box/1")
        assert r.status_code == 400 and "Pick a label colour" in r.json()["detail"]
        r = client.post("/api/save", data={"box_number": "1", "items": "[]"})
        assert r.status_code == 400
        cfg = client.get("/api/config").json()
        assert cfg["colours"] == [{"name": "Red", "css": "red"}, {"name": "Blue", "css": "blue"}]
        assert cfg["exampleBoxName"] == "Box Red 001"


def test_homebox_unreachable_gives_clear_error():
    with client_for(unreachable) as client:
        r = client.get("/api/box/1?colour=Red")
    assert r.status_code == 502
    assert "Couldn't reach Homebox at http://hb.invalid" in r.json()["detail"]


def test_homebox_unreachable_on_login():
    hb_settings = replace(Settings(), homebox_url="http://hb.invalid", homebox_api_key="")
    hb = Homebox("http://hb.invalid", username="u", password="p", transport=httpx.MockTransport(unreachable))
    with TestClient(create_app(hb_settings, homebox=hb, claude=SimpleNamespace())) as client:
        r = client.get("/api/box/1?colour=Blue")
    assert r.status_code == 502
    assert "Couldn't reach Homebox" in r.json()["detail"]


def test_save_rejects_items_that_are_not_objects():
    with client_for(unreachable) as client:
        for bad in ('["boots"]', '[1, 2]', '[{"name": "ok"}, null]', '{}', 'nope'):
            r = client.post("/api/save", data={"box_number": "1", "colour": "Red", "items": bad})
            assert r.status_code == 400, bad
            assert "JSON list" in r.json()["detail"]


def test_unlabelled_names():
    s = Settings()
    assert s.unlabelled_name(3) == "Unlabelled 003"
    assert s.unlabelled_number("Unlabelled 003") == 3
    assert s.unlabelled_number(" unlabelled 12 ") == 12
    assert s.unlabelled_number("Unlabelled stuff") is None
    assert s.unlabelled_number("Box Red 003") is None


def test_home_assistant_areas():
    import asyncio
    import json as _json
    from app import homeassistant

    def handler(request):
        assert request.headers["authorization"] == "Bearer tok"
        assert "areas()" in _json.loads(request.content)["template"]
        return httpx.Response(200, text='["Workshop", "Dressing room", " ", "Under the stairs"]')

    homeassistant._cache.update(at=0.0, areas=[])
    got = asyncio.run(homeassistant.area_names("http://ha", "tok", httpx.MockTransport(handler)))
    assert got == ["Dressing room", "Under the stairs", "Workshop"]
    assert asyncio.run(homeassistant.area_names("", "")) == []

    # Home Assistant down: keep what we had
    homeassistant._cache.update(at=0.0)
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    assert asyncio.run(homeassistant.area_names("http://ha", "tok", httpx.MockTransport(down))) == got
