"""Turn box photos + a spoken description into a draft inventory with Claude."""

from __future__ import annotations

import base64
import io
import json

import anthropic
from PIL import Image, ImageOps

# Claude sees images at up to ~1568px on the long edge; larger just costs more.
MAX_IMAGE_EDGE = 1568

SYSTEM_PROMPT = """You help someone build a household inventory of storage boxes in Homebox.

For each box you get photos (the label, the open box, items laid out) and a spoken description, \
transcribed by a phone, so expect speech-recognition mistakes, filler words and self-corrections. \
Combine both sources into the most complete, accurate list of what is in the box.

Guidelines:
- One entry per distinct kind of thing. Group identical things with a quantity \
("Paperback novels", quantity 12). Keep notably different or valuable things separate.
- Names are short and searchable, the way someone would look for them later ("Christmas lights", \
"Winter boots"). Put details (colour, brand, size, condition, model) in the description.
- Use the speaker's words for things you can't see. Use the photos for things they didn't mention, \
and for brands, models and counts you can read or see clearly.
- Don't invent things. If something is only a guess from a photo, say so in the description and set \
confidence to "low".
- If the speaker gives a rough count ("a few", "loads of"), make a sensible estimate and mention \
it was estimated.
- Suggest 1-3 broad tags per item. Prefer the existing tags you're given; only invent a new tag \
when none fits. Use title case ("Kitchen", "Books", "Electronics").
- When items already recorded for this box are listed, return only things that are new. Don't \
repeat what is already recorded.
- Ignore the box itself, packing material and tape unless the speaker says they matter.
- Fix obvious transcription errors ("Chris must lights" means "Christmas lights").
- Use "questions" for anything the person should check (an unclear word, an unreadable label, a \
possible duplicate). Leave it empty if there's nothing worth asking."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "box_summary": {
            "type": "string",
            "description": "One short line describing the box overall, e.g. 'Winter clothes and boots'.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "description": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "source": {"type": "string", "enum": ["spoken", "photo", "both"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["name", "quantity", "description", "tags", "source", "confidence"],
                "additionalProperties": False,
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["box_summary", "items", "questions"],
    "additionalProperties": False,
}


class AnalysisError(RuntimeError):
    pass


def prepare_image(data: bytes) -> tuple[str, str]:
    """Shrink and re-encode a photo for Claude. Returns (media_type, base64)."""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # respect phone rotation
    except Exception as exc:
        raise AnalysisError(f"Could not read one of the photos: {exc}") from exc
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return "image/jpeg", base64.standard_b64encode(out.getvalue()).decode()


def build_user_content(box_name: str, transcript: str, photos: list[bytes],
                       existing_tags: list[str], existing_items: list[dict]) -> list[dict]:
    content: list[dict] = []
    for i, data in enumerate(photos, 1):
        media_type, b64 = prepare_image(data)
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})

    lines = [f"Box: {box_name}", ""]
    lines.append("Spoken description:")
    lines.append(transcript.strip() or "(none given, work from the photos)")
    lines.append("")
    lines.append("Existing tags in Homebox: " + (", ".join(sorted(existing_tags)) if existing_tags else "(none yet)"))
    if existing_items:
        lines.append("")
        lines.append("Already recorded in this box (don't repeat these):")
        for item in existing_items:
            qty = item.get("quantity") or 1
            desc = f" ({item['description']})" if item.get("description") else ""
            lines.append(f"- {item['name']} x{qty:g}{desc}")
    content.append({"type": "text", "text": "\n".join(lines)})
    return content


def analyze_box(client: anthropic.Anthropic, model: str, effort: str, box_name: str,
                transcript: str, photos: list[bytes], existing_tags: list[str],
                existing_items: list[dict]) -> dict:
    if not photos and not transcript.strip():
        raise AnalysisError("Add at least one photo or a description.")

    content = build_user_content(box_name, transcript, photos, existing_tags, existing_items)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            # If a safety check wrongly declines the request, retry on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise AnalysisError("Claude rejected the API key. Check ANTHROPIC_API_KEY.") from exc
    except anthropic.RateLimitError as exc:
        raise AnalysisError("Claude is rate limiting requests. Wait a moment and try again.") from exc
    except anthropic.APIStatusError as exc:
        raise AnalysisError(f"Claude API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AnalysisError("Couldn't reach the Claude API. Check the server's internet connection.") from exc

    if response.stop_reason == "refusal":
        raise AnalysisError("Claude declined to analyse this box. Try different photos or wording.")
    if response.stop_reason == "max_tokens":
        raise AnalysisError("The inventory was too long to finish. Try splitting the box into two passes.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise AnalysisError("Claude returned an empty response.")
    try:
        draft = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalysisError("Claude returned something that wasn't valid JSON.") from exc

    for item in draft.get("items", []):
        item["quantity"] = max(1, int(item.get("quantity") or 1))
    draft["usage"] = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return draft
