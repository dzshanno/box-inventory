"""Small client for the parts of the Homebox API this app needs.

Written against Homebox v0.27, where items and locations are both "entities"
(told apart by their entity type) and labels are called "tags".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx


class HomeboxError(RuntimeError):
    pass


@dataclass
class Photo:
    filename: str
    content: bytes
    content_type: str = "image/jpeg"


# Fields copied from GET /entities/{id} into a PUT body, so an update keeps
# everything we don't mean to change.
_UPDATE_FIELDS = [
    "name", "description", "serialNumber", "modelNumber", "manufacturer",
    "warrantyDetails", "purchaseFrom", "soldTo", "soldNotes", "notes",
    "quantity", "purchasePrice", "soldPrice", "insured", "archived",
    "lifetimeWarranty", "syncChildEntityLocations", "warrantyExpires",
    "purchaseDate", "soldDate", "fields",
]


class Homebox:
    def __init__(self, base_url: str, api_key: str = "", username: str = "", password: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key and not (username and password):
            raise HomeboxError("Set HOMEBOX_API_KEY, or HOMEBOX_USERNAME and HOMEBOX_PASSWORD.")
        self._api_key = api_key
        self._username = username
        self._password = password
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._types: dict[str, str] | None = None
        self._base_url = base_url
        self._client = httpx.AsyncClient(base_url=f"{base_url}/api/v1", timeout=60, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    # ---- auth -------------------------------------------------------------

    async def _auth_header(self) -> str:
        if self._api_key:
            key = self._api_key
            return key if key.lower().startswith("bearer ") else f"Bearer {key}"
        async with self._lock:
            if not self._token:
                await self._login()
            return self._token  # type: ignore[return-value]

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise HomeboxError(f"Homebox at {self._base_url} took too long to answer.") from exc
        except httpx.RequestError as exc:
            raise HomeboxError(f"Couldn't reach Homebox at {self._base_url}. Is it running, and is HOMEBOX_URL right?") from exc

    async def _login(self) -> None:
        r = await self._send("POST", "/users/login", json={
            "username": self._username, "password": self._password, "stayLoggedIn": True,
        })
        if r.status_code != 200:
            raise HomeboxError(f"Homebox login failed ({r.status_code}). Check the username and password.")
        self._token = r.json()["token"]

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(2):
            headers = {"Authorization": await self._auth_header()}
            r = await self._send(method, path, headers=headers, **kwargs)
            if r.status_code == 401 and not self._api_key and attempt == 0:
                self._token = None  # session expired; log in again once
                continue
            if r.status_code == 401:
                raise HomeboxError("Homebox rejected the credentials (401). Is the API key valid and not expired?")
            if r.status_code >= 400:
                raise HomeboxError(f"Homebox {method} {path} failed ({r.status_code}): {r.text[:300]}")
            return r
        raise HomeboxError("Homebox authentication failed.")

    async def _get(self, path: str, **params):
        return (await self._request("GET", path, params=params)).json()

    # ---- entity types and tags ---------------------------------------------

    async def _entity_types(self) -> dict[str, str]:
        """Return {"location": id, "item": id} using Homebox's built-in types."""
        if self._types is None:
            types = await self._get("/entity-types")
            loc = next((t for t in types if t["isLocation"] and t["name"] == "Location"), None) \
                or next((t for t in types if t["isLocation"]), None)
            item = next((t for t in types if not t["isLocation"] and t["name"] == "Item"), None) \
                or next((t for t in types if not t["isLocation"]), None)
            if not loc or not item:
                raise HomeboxError("Could not find Homebox's Location and Item entity types.")
            self._types = {"location": loc["id"], "item": item["id"]}
        return self._types

    async def list_tags(self) -> list[dict]:
        return await self._get("/tags")

    async def ensure_tags(self, names: list[str]) -> dict[str, str]:
        """Map each tag name to its id, creating any that don't exist (case-insensitive match)."""
        existing = {t["name"].strip().lower(): t["id"] for t in await self.list_tags()}
        result: dict[str, str] = {}
        for name in names:
            name = name.strip()
            if not name:
                continue
            key = name.lower()
            if key not in existing:
                created = (await self._request("POST", "/tags", json={"name": name[:255]})).json()
                existing[key] = created["id"]
            result[name] = existing[key]
        return result

    # ---- locations --------------------------------------------------------

    async def find_location(self, name: str, parent_id: str | None) -> dict | None:
        """Find a location by exact name (search in Homebox is fuzzy, so filter here)."""
        page = await self._get("/entities", q=name, isLocation="true", pageSize=100)
        for e in page.get("items", []):
            if e["name"].strip().lower() != name.strip().lower():
                continue
            e_parent = (e.get("parent") or {}).get("id")
            if parent_id is None or e_parent == parent_id:
                return e
        return None

    async def create_entity(self, name: str, kind: str, parent_id: str | None = None,
                            quantity: float = 0, description: str = "",
                            tag_ids: list[str] | None = None) -> dict:
        types = await self._entity_types()
        body = {
            "name": name[:255],
            "entityTypeId": types[kind],
            "quantity": quantity,
            "description": description[:1000],
            "tagIds": tag_ids or [],
        }
        if parent_id:
            body["parentId"] = parent_id
        return (await self._request("POST", "/entities", json=body)).json()

    async def ensure_location(self, name: str, parent_id: str | None) -> tuple[dict, bool]:
        found = await self.find_location(name, parent_id)
        if found:
            return found, False
        return await self.create_entity(name, "location", parent_id), True

    async def get_entity(self, entity_id: str) -> dict:
        return await self._get(f"/entities/{entity_id}")

    async def children(self, entity_id: str) -> list[dict]:
        items: list[dict] = []
        page_no = 1
        while True:
            page = await self._get("/entities", parentIds=entity_id, pageSize=100, page=page_no)
            items.extend(page.get("items", []))
            if len(items) >= page.get("total", 0) or not page.get("items"):
                return items
            page_no += 1

    async def update_entity(self, entity_id: str, current: dict | None = None, **changes) -> dict:
        """Change some fields of an entity, keeping everything else as it is."""
        current = current or await self.get_entity(entity_id)
        body = {k: current.get(k) for k in _UPDATE_FIELDS}
        body["fields"] = body.get("fields") or []
        body["id"] = entity_id
        body["entityTypeId"] = current["entityType"]["id"]
        body["tagIds"] = [t["id"] for t in current.get("tags") or []]
        parent = (current.get("parent") or {}).get("id")
        if parent:
            body["parentId"] = parent
        body.update(changes)
        return (await self._request("PUT", f"/entities/{entity_id}", json=body)).json()

    async def append_notes(self, entity_id: str, text: str) -> None:
        current = await self.get_entity(entity_id)
        existing = (current.get("notes") or "").strip()
        await self.update_entity(entity_id, current, notes=f"{existing}\n\n{text}".strip() if existing else text)

    async def find_locations(self, query: str, parent_id: str | None) -> list[dict]:
        """All locations whose name contains `query`, inside parent_id (any parent if None)."""
        found, page_no = [], 1
        while True:
            page = await self._get("/entities", q=query, isLocation="true", pageSize=100, page=page_no)
            for e in page.get("items", []):
                if parent_id is None or (e.get("parent") or {}).get("id") == parent_id:
                    found.append(e)
            if page_no * 100 >= page.get("total", 0) or not page.get("items"):
                return found
            page_no += 1

    async def search(self, query: str, limit: int = 300) -> list[dict]:
        """Entities (items and locations) matching `query`, as Homebox's search finds them."""
        found, page_no = [], 1
        while len(found) < limit:
            page = await self._get("/entities", q=query, pageSize=100, page=page_no)
            found.extend(page.get("items", []))
            if page_no * 100 >= page.get("total", 0) or not page.get("items"):
                break
            page_no += 1
        return found[:limit]

    async def delete_entity(self, entity_id: str) -> None:
        await self._request("DELETE", f"/entities/{entity_id}")

    async def add_photo(self, entity_id: str, photo: Photo, primary: bool = False) -> None:
        await self._request(
            "POST", f"/entities/{entity_id}/attachments",
            files={"file": (photo.filename, photo.content, photo.content_type)},
            data={"type": "photo", "name": photo.filename, "primary": "true" if primary else "false"},
        )
