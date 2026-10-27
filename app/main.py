"""Box Inventory: photograph a box, describe it, and save the contents to Homebox."""

from __future__ import annotations

import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .analyze import AnalysisError, analyze_box, read_label
from .config import Settings, settings as default_settings
from .homebox import Homebox, HomeboxError, Photo

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

    async def parent_id(request: Request, create: bool) -> str | None:
        if not settings.parent_location:
            return None
        if create:
            loc, _ = await hb(request).ensure_location(settings.parent_location, None)
            return loc["id"]
        loc = await hb(request).find_location(settings.parent_location, None)
        return loc["id"] if loc else ""  # "" = parent doesn't exist yet, so no box can either

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
            "parentLocation": settings.parent_location,
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
        if not (box.get("entityType") or {}).get("isLocation", True):
            raise HTTPException(400, "That isn't a box.")
        pid = await parent_id(request, create=False)
        if pid is not None and (box.get("parent") or {}).get("id") != pid:
            raise HTTPException(400, f"That box isn't in “{settings.parent_location}”.")
        return box

    async def box_info(request: Request, name: str, box: dict | None) -> dict:
        unlabelled = settings.unlabelled_number(name) is not None
        if not box:
            return {"boxName": name, "exists": False, "unlabelled": unlabelled, "items": [], "photoCount": 0}
        full = box if "attachments" in box else await hb(request).get_entity(box["id"])
        items = [summarize(i) for i in await hb(request).children(box["id"])]
        photos = [a for a in full.get("attachments") or [] if a.get("type") == "photo"]
        return {
            "boxName": full["name"], "exists": True, "unlabelled": unlabelled, "id": box["id"],
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
            pid = await parent_id(request, create=False)
            return name, (None if pid == "" else await hb(request).find_location(name, pid))
        return None, None

    async def unlabelled_boxes(request: Request) -> list[dict]:
        pid = await parent_id(request, create=False)
        if pid == "":
            return []
        found = await hb(request).find_locations(settings.unlabelled_prefix, pid)
        boxes = [b for b in found if settings.unlabelled_number(b["name"]) is not None]
        return sorted(boxes, key=lambda b: settings.unlabelled_number(b["name"]))

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
            box = await box_info(request, name, found)
        return {"label": label, "box": box}

    @app.get("/api/unlabelled")
    async def list_unlabelled(request: Request):
        boxes = []
        for b in await unlabelled_boxes(request):
            items = await hb(request).children(b["id"])
            boxes.append({"id": b["id"], "boxName": b["name"], "itemCount": len(items),
                          "items": [i["name"] for i in items[:6]], "link": box_link(b["id"])})
        return {"boxes": boxes}

    @app.post("/api/box-by-id/{box_id}/label")
    async def label_box(box_id: str, request: Request, box_number: str = Form(...), colour: str = Form("")):
        """Give a box a (new) label: renames it in Homebox."""
        box = await box_by_id(request, box_id)
        name = box_name(box_number, colour)
        if name == box["name"]:
            return await box_info(request, name, box)
        pid = await parent_id(request, create=False)
        clash = await hb(request).find_location(name, pid or None)
        if clash and clash["id"] != box_id:
            raise HTTPException(409, f"{name} already exists. Check the label, or open {name} to see what's in it.")
        old = box["name"]
        await hb(request).update_entity(box_id, box, name=name)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        await hb(request).append_notes(box_id, f"[{stamp}] Labelled {name} (was {old}).")
        log.info("Relabelled %s as %s", old, name)
        return await box_info(request, name, await hb(request).get_entity(box_id))

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
                   items: str = Form("[]"), transcript: str = Form(""), box_summary: str = Form(""),
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

        pid = await parent_id(request, create=True)
        if box:
            created = False
        elif name:
            box, created = await client.ensure_location(name, pid)
        else:
            taken = [settings.unlabelled_number(b["name"]) for b in await unlabelled_boxes(request)]
            name = settings.unlabelled_name(max(taken, default=0) + 1)
            box, created = await client.create_entity(name, "location", pid), True
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

        log.info("Saved %s: %d items, %d photos", name, len(saved), len(pics))
        return {"boxName": name, "boxId": box_id, "created": created, "itemsSaved": len(saved),
                "photosSaved": len(pics), "link": box_link(box_id)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
