"""Box Inventory: photograph a box, describe it, and save the contents to Homebox."""

from __future__ import annotations

import base64
import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .analyze import AnalysisError, analyze_box, locate_boxes, read_label
from .config import Settings, settings as default_settings
from .homeassistant import area_names
from .homebox import Homebox, HomeboxError, Photo
from .search import box_matches, claude_search, fuzzy_search

log = logging.getLogger("box-inventory")
STATIC = Path(__file__).parent / "static"
MAX_PHOTOS = 8
MAX_PHOTO_BYTES = 15 * 1024 * 1024


def create_app(settings: Settings = default_settings, homebox: Homebox | None = None,
               claude: anthropic.Anthropic | None = None) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.homebox = homebox or Homebox(
            settings.homebox_url, settings.homebox_api_key,
            settings.homebox_username, settings.homebox_password,
        )
        app.state.claude = claude or anthropic.Anthropic()
        yield
        await app.state.homebox.close()

    app = FastAPI(title="Box Inventory", lifespan=lifespan)

    # ---- optional password (HTTP basic auth, any username) ----------------
    @app.middleware("http")
    async def password_gate(request: Request, call_next):
        if settings.app_password and request.url.path != "/healthz":
            ok = False
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(pw.encode(), settings.app_password.encode())
                except Exception:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Box Inventory"'})
        return await call_next(request)

    @app.exception_handler(HomeboxError)
    async def homebox_error(_: Request, exc: HomeboxError):
        log.warning("Homebox error: %s", exc)
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(AnalysisError)
    async def analysis_error(_: Request, exc: AnalysisError):
        log.warning("Analysis error: %s", exc)
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def hb(request: Request) -> Homebox:
        return request.app.state.homebox

    # ---- places and boxes ------------------------------------------------
    # Places (Workshop, Lockup…) are Homebox locations at the top level, or inside STORAGE_ROOT.
    # Boxes are locations with a box name (Box Red 032, Unlabelled 003) inside a place.
    # Box names are unique everywhere, so a box keeps its name when it moves.
    # One listing of every location serves all of this; it's kept for 30 seconds and
    # dropped whenever this app changes something.
    layout_cache: dict = {"at": 0.0, "data": None}
    catalogue_cache: dict = {"at": 0.0, "data": None}

    def forget_catalogue() -> None:
        layout_cache["data"] = None
        catalogue_cache["data"] = None

    def parent_of(e: dict) -> str | None:
        return (e.get("parent") or {}).get("id")

    async def layout(request: Request) -> dict:
        if layout_cache["data"] and time.monotonic() - layout_cache["at"] < 30:
            return layout_cache["data"]
        locs = await hb(request).find_locations("", None)
        root: str | None = None
        if settings.storage_root:
            want = settings.storage_root.strip().lower()
            found = next((l for l in locs if not parent_of(l) and l["name"].strip().lower() == want), None)
            root = found["id"] if found else ""  # "" = not created yet, so no places either
        places = {} if root == "" else {
            l["id"]: l for l in locs
            if parent_of(l) == root and l["id"] != root and not settings.is_box_name(l["name"])}
        boxes = {l["id"]: l for l in locs if parent_of(l) in places and settings.is_box_name(l["name"])}
        data = {"root": root, "places": places, "boxes": boxes,
                "by_name": {b["name"].strip().lower(): b for b in boxes.values()}}
        layout_cache.update(at=time.monotonic(), data=data)
        return data

    async def ensure_place(request: Request, name: str) -> dict:
        name = " ".join(name.split())[:255]
        if not name:
            raise HTTPException(400, "Give the place a name.")
        if settings.is_box_name(name):
            raise HTTPException(400, f"“{name}” looks like a box name. Pick another name for the place.")
        lay = await layout(request)
        for p in lay["places"].values():
            if p["name"].strip().lower() == name.lower():
                return p
        root = lay["root"]
        if root == "":
            root_loc, _ = await hb(request).ensure_location(settings.storage_root, None)
            root = root_loc["id"]
        place = await hb(request).create_entity(name, "location", root)
        log.info("Added place %s", name)
        forget_catalogue()
        return place

    async def pick_place(request: Request, location_id: str) -> dict:
        """The place a form asked for, or the default place."""
        lay = await layout(request)
        if location_id.strip():
            if location_id.strip() not in lay["places"]:
                raise HTTPException(400, "That place no longer exists. Pick another.")
            return lay["places"][location_id.strip()]
        return await ensure_place(request, settings.default_place)

    LAST_SEEN = "Last seen"

    def last_seen(entity: dict) -> dict | None:
        """{"at", "place", "how", "spot"} from the box's "Last seen" field."""
        for f in entity.get("fields") or []:
            if f.get("name") == LAST_SEEN and f.get("textValue"):
                parts = [x.strip() for x in str(f["textValue"]).split(" · ")]
                parts += [""] * (4 - len(parts))
                return {"at": parts[0], "place": parts[1], "how": parts[2], "spot": " · ".join(parts[3:]).strip(" ·")}
        return None

    async def mark_seen(request: Request, box_id: str, how: str, spot: str = "") -> None:
        """Record when and where a box was last seen, in a "Last seen" field Homebox shows too."""
        current = await hb(request).get_entity(box_id)
        place = (current.get("parent") or {}).get("name", "")
        text = " · ".join(x for x in (datetime.now().strftime("%Y-%m-%d %H:%M"), place, how, spot.strip()) if x)
        fields = [f for f in current.get("fields") or [] if f.get("name") != LAST_SEEN]
        fields.append({"name": LAST_SEEN, "type": "text", "textValue": text})
        await hb(request).update_entity(box_id, current, fields=fields)

    def box_name(number: str, colour: str) -> str:
        try:
            return settings.box_name(number, colour)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def box_link(box_id: str) -> str | None:
        return f"{settings.homebox_public_url}/location/{box_id}" if settings.homebox_public_url else None

    async def read_photos(photos: list[UploadFile]) -> list[Photo]:
        if len(photos) > MAX_PHOTOS:
            raise HTTPException(400, f"Up to {MAX_PHOTOS} photos per box, please.")
        out = []
        for i, upload in enumerate(photos, 1):
            data = await upload.read()
            if not data:
                continue
            if len(data) > MAX_PHOTO_BYTES:
                raise HTTPException(400, f"Photo {i} is too large.")
            out.append(Photo(upload.filename or f"photo-{i}.jpg", data, upload.content_type or "image/jpeg"))
        return out

    def summarize(item: dict) -> dict:
        return {
            "id": item["id"],
            "name": item["name"],
            "quantity": item.get("quantity") or 1,
            "description": item.get("description") or "",
            "tags": [t["name"] for t in item.get("tags") or []],
        }

    # ---- routes ----------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    async def config():
        return {
            "defaultPlace": settings.default_place,
            "colours": [{"name": n, "css": css} for n, css in settings.box_colours],
            "exampleBoxName": settings.box_name("1", settings.box_colours[0][0] if settings.box_colours else ""),
            "maxPhotos": MAX_PHOTOS,
        }

    async def box_by_id(request: Request, box_id: str) -> dict:
        """Fetch a box by its Homebox id, checking it's one of ours."""
        try:
            box = await hb(request).get_entity(box_id)
        except HomeboxError:
            raise HTTPException(404, "That box no longer exists in Homebox.")
        if box_id not in (await layout(request))["boxes"]:
            forget_catalogue()  # maybe it's new: look again
            if box_id not in (await layout(request))["boxes"]:
                raise HTTPException(400, "That isn't one of your boxes.")
        return box

    async def box_info(request: Request, name: str, box: dict | None) -> dict:
        unlabelled = settings.unlabelled_number(name) is not None
        if not box:
            return {"boxName": name, "exists": False, "unlabelled": unlabelled, "items": [], "photoCount": 0}
        full = box if "attachments" in box else await hb(request).get_entity(box["id"])
        items = [summarize(i) for i in await hb(request).children(box["id"])]
        photos = [a for a in full.get("attachments") or [] if a.get("type") == "photo"]
        parent = full.get("parent") or {}
        return {
            "boxName": full["name"], "exists": True, "unlabelled": unlabelled, "id": box["id"],
            "place": {"id": parent.get("id"), "name": parent.get("name", "")}, "lastSeen": last_seen(full),
            "link": box_link(box["id"]), "items": items, "photoCount": len(photos), "notes": full.get("notes") or "",
        }

    async def resolve_box(request: Request, number: str, colour: str, box_id: str) -> tuple[str | None, dict | None]:
        """Work out which box a request is about. Returns (name, box).

        By id: an existing box (labelled or not). By number and colour: that box, which may not exist yet.
        Neither: a new box without a label, which gets its name when it's saved (name None)."""
        if box_id.strip():
            box = await box_by_id(request, box_id.strip())
            return box["name"], box
        if number.strip():
            name = box_name(number, colour)
            return name, (await layout(request))["by_name"].get(name.lower())
        return None, None

    async def unlabelled_boxes(request: Request) -> list[dict]:
        boxes = [b for b in (await layout(request))["boxes"].values() if settings.unlabelled_number(b["name"]) is not None]
        return sorted(boxes, key=lambda b: settings.unlabelled_number(b["name"]))

    @app.get("/api/places")
    async def list_places(request: Request):
        """Places boxes can live in, plus Home Assistant areas that could become places."""
        lay = await layout(request)
        counts: dict[str, int] = {}
        for b in lay["boxes"].values():
            counts[parent_of(b)] = counts.get(parent_of(b), 0) + 1
        places = sorted(({"id": p["id"], "name": p["name"], "boxCount": counts.get(p["id"], 0)}
                         for p in lay["places"].values()), key=lambda p: p["name"].lower())
        have = {p["name"].strip().lower() for p in places}
        areas = await area_names(settings.ha_url, settings.ha_token)
        return {"places": places, "suggestions": [a for a in areas if a.lower() not in have],
                "defaultPlace": settings.default_place}

    @app.post("/api/places")
    async def add_place(request: Request, name: str = Form(...)):
        place = await ensure_place(request, name)
        return {"id": place["id"], "name": place["name"]}

    @app.post("/api/box-by-id/{box_id}/move")
    async def move_box(box_id: str, request: Request, location_id: str = Form(...)):
        """Move a box to another place."""
        box = await box_by_id(request, box_id)
        place = await pick_place(request, location_id)
        old = (box.get("parent") or {}).get("name", "")
        if parent_of(box) != place["id"]:
            await hb(request).update_entity(box_id, box, parentId=place["id"])
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            await hb(request).append_notes(box_id, f"[{stamp}] Moved from {old} to {place['name']}.")
            await mark_seen(request, box_id, "moved here")
            log.info("Moved %s from %s to %s", box["name"], old, place["name"])
            forget_catalogue()
        return await box_info(request, box["name"], await hb(request).get_entity(box_id))

    @app.get("/api/box/{number}")
    async def get_box(number: str, request: Request, colour: str = ""):
        name, box = await resolve_box(request, number, colour, "")
        return await box_info(request, name, box)

    @app.get("/api/box-by-id/{box_id}")
    async def get_box_by_id(box_id: str, request: Request):
        box = await box_by_id(request, box_id)
        return await box_info(request, box["name"], box)

    @app.post("/api/read-label")
    async def read_label_route(request: Request, photo: UploadFile = File(...)):
        """Read the colour and number off a label photo, and look that box up."""
        pics = await read_photos([photo])
        if not pics:
            raise HTTPException(400, "The photo was empty.")
        colours = [n for n, _ in settings.box_colours]
        label = await run_in_threadpool(read_label, request.app.state.claude, settings.claude_model,
                                        pics[0].content, colours)
        log.info("Read label: %s", label)
        box = None
        if label["has_label"] and (label["colour"] or not colours):
            name, found = await resolve_box(request, label["number"], label["colour"], "")
            if found and label["confidence"] == "high":
                await mark_seen(request, found["id"], "label photographed")
            box = await box_info(request, name, found)
        return {"label": label, "box": box}

    @app.get("/api/unlabelled")
    async def list_unlabelled(request: Request):
        boxes = []
        for b in await unlabelled_boxes(request):
            items = await hb(request).children(b["id"])
            boxes.append({"id": b["id"], "boxName": b["name"], "itemCount": len(items),
                          "place": (b.get("parent") or {}).get("name", ""),
                          "items": [i["name"] for i in items[:6]], "link": box_link(b["id"])})
        return {"boxes": boxes}

    @app.post("/api/box-by-id/{box_id}/label")
    async def label_box(box_id: str, request: Request, box_number: str = Form(...), colour: str = Form("")):
        """Give a box a (new) label: renames it in Homebox."""
        box = await box_by_id(request, box_id)
        name = box_name(box_number, colour)
        if name == box["name"]:
            return await box_info(request, name, box)
        clash = (await layout(request))["by_name"].get(name.lower())
        if clash and clash["id"] != box_id:
            raise HTTPException(409, f"{name} already exists. Check the label, or open {name} to see what's in it.")
        old = box["name"]
        await hb(request).update_entity(box_id, box, name=name)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        await hb(request).append_notes(box_id, f"[{stamp}] Labelled {name} (was {old}).")
        log.info("Relabelled %s as %s", old, name)
        forget_catalogue()
        return await box_info(request, name, await hb(request).get_entity(box_id))

    async def catalogue(request: Request) -> tuple[list[dict], list[dict]]:
        """The whole inventory, flattened, for searching. Kept for a minute (see layout())."""
        if catalogue_cache["data"] and time.monotonic() - catalogue_cache["at"] < 60:
            return catalogue_cache["data"]
        lay = await layout(request)
        boxes, places = lay["boxes"], lay["places"]

        def place_name(box: dict) -> str:
            return (places.get(parent_of(box)) or {}).get("name", "")

        items = []
        for e in await hb(request).search("", limit=20000):
            parent = parent_of(e)
            if parent in boxes and e["id"] not in boxes:
                items.append({**summarize(e), "boxId": parent, "boxName": boxes[parent]["name"],
                              "place": place_name(boxes[parent])})
        data = (items, [{"id": b["id"], "name": b["name"], "place": place_name(b)} for b in boxes.values()])
        catalogue_cache.update(at=time.monotonic(), data=data)
        return data

    async def group_by_box(request: Request, items: list[dict], boxes: list[dict] = ()) -> list[dict]:
        """Results grouped by box, in the order the items came (best first), with where each box is."""
        groups: dict[str, dict] = {}
        for b in boxes:
            groups[b["id"]] = {"boxId": b["id"], "boxName": b["name"], "boxMatches": True, "items": []}
        for i in items:
            g = groups.setdefault(i["boxId"], {"boxId": i["boxId"], "boxName": i["boxName"],
                                               "boxMatches": False, "items": []})
            g["items"].append({k: v for k, v in i.items() if k not in ("boxId", "boxName", "place", "score")})
        groups_list = list(groups.values())[:30]
        # Where each box is and when it was last seen (one Homebox read per box, in parallel)
        entities = await asyncio.gather(*(hb(request).get_entity(g["boxId"]) for g in groups_list),
                                        return_exceptions=True)
        for g, e in zip(groups_list, entities):
            g["link"] = box_link(g["boxId"])
            g["unlabelled"] = settings.unlabelled_number(g["boxName"]) is not None
            ok = isinstance(e, dict)
            g["place"] = (e.get("parent") or {}).get("name", "") if ok else ""
            g["lastSeen"] = last_seen(e) if ok else None
        return groups_list

    @app.get("/api/search")
    async def search(request: Request, q: str = ""):
        """Quick search as you type: forgiving word matching on items and box names."""
        q = q.strip()
        if len(q) < 2:
            return {"query": q, "results": []}
        items, boxes = await catalogue(request)
        return {"query": q, "results": await group_by_box(request, fuzzy_search(q, items)[:200], box_matches(q, boxes))}

    @app.get("/api/search/claude")
    async def search_claude(request: Request, q: str = ""):
        """Search by meaning: Claude reads the whole inventory and finds things described differently."""
        q = q.strip()
        if len(q) < 2:
            raise HTTPException(400, "Type what you're looking for first.")
        items, _ = await catalogue(request)
        res = await run_in_threadpool(claude_search, request.app.state.claude,
                                      settings.search_model or settings.claude_model, q, items)
        log.info("Claude search %r: %d matches, usage %s", q, len(res["matches"]), res["usage"])
        return {"query": q, "results": await group_by_box(request, res["matches"]), "note": res["note"]}

    async def our_item(request: Request, item_id: str) -> dict:
        try:
            item = await hb(request).get_entity(item_id)
        except HomeboxError:
            raise HTTPException(404, "That item no longer exists in Homebox.")
        parent = (item.get("parent") or {}).get("id")
        if not parent or (item.get("entityType") or {}).get("isLocation"):
            raise HTTPException(400, "That isn't an item in a box.")
        await box_by_id(request, parent)  # checks the box is one of ours
        return item

    @app.put("/api/item/{item_id}")
    async def edit_item(item_id: str, request: Request, name: str = Form(...), quantity: str = Form("1"),
                        description: str = Form(""), tags: str = Form("")):
        item = await our_item(request, item_id)
        if not name.strip():
            raise HTTPException(400, "The item needs a name.")
        try:
            qty = max(1.0, float(quantity))
        except ValueError:
            qty = 1.0
        tag_names = [t.strip() for t in tags.split(",") if t.strip()]
        tag_ids = await hb(request).ensure_tags(tag_names)
        updated = await hb(request).update_entity(
            item_id, item, name=name.strip()[:255], quantity=qty, description=description.strip()[:1000],
            tagIds=list(dict.fromkeys(tag_ids[t] for t in tag_names)),
        )
        forget_catalogue()
        return summarize(updated)

    @app.delete("/api/item/{item_id}")
    async def delete_item(item_id: str, request: Request):
        item = await our_item(request, item_id)
        await hb(request).delete_entity(item_id)
        log.info("Deleted item %s (%s)", item["name"], item_id)
        forget_catalogue()
        return {"deleted": item_id}

    @app.post("/api/locate")
    async def locate(request: Request, box_ids: str = Form(...), photo: UploadFile = File(...)):
        """Spot one or more boxes (comma-separated ids) in a wide photo of the storage area."""
        ids = list(dict.fromkeys(i.strip() for i in box_ids.split(",") if i.strip()))
        if not ids:
            raise HTTPException(400, "Say which box to look for.")
        if len(ids) > 12:
            raise HTTPException(400, "Look for up to 12 boxes at a time.")
        targets = []
        for box_id in ids:
            box = await box_by_id(request, box_id)
            label = settings.parse_box_name(box["name"])
            if label:
                colour, number = label
                targets.append((box, f"{colour.lower()} {number}" if colour else number))
        if not targets:
            raise HTTPException(400, "Boxes without a label can't be spotted in a photo.")
        pics = await read_photos([photo])
        if not pics:
            raise HTTPException(400, "The photo was empty.")
        res = await run_in_threadpool(locate_boxes, request.app.state.claude, settings.claude_model,
                                      pics[0].content, [t for _, t in targets])
        results = [{"boxId": box["id"], "boxName": box["name"], **r}
                   for (box, _), r in zip(targets, res["results"])]
        log.info("Located %s: %s", [t for _, t in targets], [(r["target"], r["found"]) for r in results])
        for r in results:
            if r["found"] and r["confidence"] != "low":
                await mark_seen(request, r["boxId"], "spotted in a photo", r["where"])
        return {"results": results, "seen": res["seen"]}

    @app.post("/api/analyze")
    async def analyze(request: Request, box_number: str = Form(""), colour: str = Form(""), box_id: str = Form(""),
                      transcript: str = Form(""), photos: list[UploadFile] = File(default=[])):
        name, box = await resolve_box(request, box_number, colour, box_id)
        pics = await read_photos(photos)
        tags = [t["name"] for t in await hb(request).list_tags()]
        existing = [summarize(i) for i in await hb(request).children(box["id"])] if box else []
        draft = await run_in_threadpool(
            analyze_box, request.app.state.claude, settings.claude_model, settings.claude_effort,
            name or "a new box without a label yet", transcript, [p.content for p in pics], tags, existing,
        )
        log.info("Analysed %s: %d items, usage %s", name or "new unlabelled box",
                 len(draft.get("items", [])), draft.get("usage"))
        return draft

    @app.post("/api/save")
    async def save(request: Request, box_number: str = Form(""), colour: str = Form(""), box_id: str = Form(""),
                   location_id: str = Form(""), items: str = Form("[]"), transcript: str = Form(""),
                   box_summary: str = Form(""),
                   photos: list[UploadFile] = File(default=[])):
        try:
            item_list = json.loads(items)
            assert isinstance(item_list, list) and all(isinstance(i, dict) for i in item_list)
        except Exception:
            raise HTTPException(400, "items must be a JSON list of objects")
        name, box = await resolve_box(request, box_number, colour, box_id)
        item_list = [i for i in item_list if str(i.get("name", "")).strip()]
        pics = await read_photos(photos)
        client = hb(request)

        moved_from = None
        if box:
            created = False
            # You're standing with the box, so if you picked a different place, it has moved.
            if location_id.strip() and location_id.strip() != parent_of(box):
                place = await pick_place(request, location_id)
                moved_from = (box.get("parent") or {}).get("name", "")
                box = await client.update_entity(box["id"], await client.get_entity(box["id"]), parentId=place["id"])
        else:
            place = await pick_place(request, location_id)
            if not name:
                taken = [settings.unlabelled_number(b["name"]) for b in await unlabelled_boxes(request)]
                name = settings.unlabelled_name(max(taken, default=0) + 1)
            box, created = await client.create_entity(name, "location", place["id"]), True
        forget_catalogue()
        box_id = box["id"]

        all_tags = sorted({t.strip() for i in item_list for t in i.get("tags") or [] if t.strip()})
        tag_ids = await client.ensure_tags(all_tags)

        saved = []
        for item in item_list:
            qty = item.get("quantity") or 1
            try:
                qty = max(1, float(qty))
            except (TypeError, ValueError):
                qty = 1
            ids = [tag_ids[t.strip()] for t in item.get("tags") or [] if t.strip() in tag_ids]
            out = await client.create_entity(
                str(item["name"]).strip(), "item", parent_id=box_id, quantity=qty,
                description=str(item.get("description") or ""), tag_ids=ids,
            )
            saved.append(out["id"])

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for i, photo in enumerate(pics, 1):
            ext = Path(photo.filename).suffix or ".jpg"
            photo.filename = f"{name} {stamp.replace(':', '')} ({i}){ext}"
            await client.add_photo(box_id, photo, primary=created and i == 1)

        if settings.save_transcript and (transcript.strip() or box_summary.strip()):
            parts = [f"[{stamp}]"]
            if box_summary.strip():
                parts.append(f"Summary: {box_summary.strip()}")
            if transcript.strip():
                parts.append(f"Spoken description: {transcript.strip()}")
            await client.append_notes(box_id, "\n".join(parts))

        if moved_from is not None:
            await client.append_notes(box_id, f"[{stamp}] Moved from {moved_from}.")
        await mark_seen(request, box_id, "photographed" if pics else "updated")
        log.info("Saved %s: %d items, %d photos", name, len(saved), len(pics))
        forget_catalogue()
        place_name = ((await client.get_entity(box_id)).get("parent") or {}).get("name", "")
        return {"boxName": name, "boxId": box_id, "created": created, "itemsSaved": len(saved),
                "photosSaved": len(pics), "link": box_link(box_id), "place": place_name, "movedFrom": moved_from}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
