"""Read area (room) names from Home Assistant, to offer as places for boxes."""

from __future__ import annotations

import json
import logging
import time

import httpx

log = logging.getLogger("box-inventory")

# Home Assistant's template API returns every area name in one request.
AREAS_TEMPLATE = "{{ areas() | map('area_name') | list | tojson }}"

_cache: dict = {"at": 0.0, "areas": []}


async def area_names(url: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Area names from Home Assistant, or [] if it isn't set up or can't be reached. Cached for 10 minutes."""
    if not (url and token):
        return []
    if _cache["at"] and time.monotonic() - _cache["at"] < 600:
        return _cache["areas"]
    try:
        async with httpx.AsyncClient(timeout=8, transport=transport) as client:
            r = await client.post(f"{url}/api/template", json={"template": AREAS_TEMPLATE},
                                  headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        areas = sorted({str(a).strip() for a in json.loads(r.text) if str(a).strip()}, key=str.lower)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("Couldn't read areas from Home Assistant: %s", exc)
        return _cache["areas"]  # keep showing what we had
    _cache.update(at=time.monotonic(), areas=areas)
    return areas
