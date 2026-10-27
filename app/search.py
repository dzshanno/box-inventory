"""Finding things: forgiving word matching, plus Claude for "same thing, different words"."""

from __future__ import annotations

import json
import re
import unicodedata

import anthropic
from rapidfuzz import fuzz

from .analyze import AnalysisError

# Words that don't help find anything.
STOP_WORDS = {"a", "an", "and", "the", "of", "some", "my", "our", "in", "for", "with", "where", "is", "are",
              "what", "which", "any", "all", "i", "me", "find", "looking", "box", "boxes"}


def _stem(word: str) -> str:
    """Very small English stemmer, enough that "lights" finds "light" and "boxes" finds "box"."""
    if word.isdigit():
        return word.lstrip("0") or "0"
    for suffix, repl in (("ies", "y"), ("ches", "ch"), ("shes", "sh"), ("xes", "x"), ("ses", "s")):
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            return word[: -len(suffix)] + repl
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text)]


def query_words(query: str) -> list[str]:
    ws = words(query)
    kept = [w for w in ws if w not in STOP_WORDS]
    return kept or ws


def _word_score(q: str, w: str) -> float:
    """0-100: how well one query word matches one stored word (typos and half-typed words allowed)."""
    if q == w:
        return 100
    if q.isdigit() or w.isdigit():
        return 0  # numbers must match exactly: box 4 is not box 40
    if len(q) >= 3 and w.startswith(q):
        return 92  # still typing
    if len(q) >= 4 and q.startswith(w) and len(w) >= 4:
        return 85
    if len(q) < 4:
        return 0
    score = fuzz.ratio(q, w)
    need = 75 if len(q) <= 5 else 80  # about one typo per five letters
    return score if score >= need else 0


def _best(q: str, ws: list[str]) -> float:
    return max((_word_score(q, w) for w in ws), default=0)


def fuzzy_search(query: str, catalogue: list[dict]) -> list[dict]:
    """Match every query word against each item (name, details, tags) and box name.

    catalogue: [{"id", "name", "quantity", "description", "tags", "boxId", "boxName"}].
    Returns matching items with a "score", best first. Every query word has to match somewhere.
    """
    qs = query_words(query)
    if not qs:
        return []
    out = []
    for item in catalogue:
        name, extra = words(item["name"]), words(item.get("description", "") + " " + " ".join(item.get("tags", [])))
        box = words(item["boxName"] + " " + item.get("place", ""))
        total = 0.0
        for q in qs:
            s = max(_best(q, name), 0.85 * _best(q, extra), 0.9 * _best(q, box))
            if s == 0:
                break
            total += s
        else:
            # Items whose own name matches rank above ones that only match via their box
            own = sum(max(_best(q, name), 0.85 * _best(q, extra)) for q in qs)
            out.append({**item, "score": round(total / len(qs) + own / (10 * len(qs)), 1)})
    return sorted(out, key=lambda i: -i["score"])


def box_matches(query: str, boxes: list[dict]) -> list[dict]:
    """Boxes whose name or place matches every query word, e.g. "red 47", "unlabelled 3", "workshop"."""
    qs = query_words(query)
    if not qs:
        return []
    return [b for b in boxes if all(_best(q, words(b["name"] + " " + b.get("place", ""))) for q in qs)]


SEARCH_PROMPT = """You help someone find things in the inventory of their storage boxes.

They'll describe what they want in their own words, which are often not the words used when the item \
was stored: "fairy lights" might be stored as "Christmas lights", "drill" as "cordless screwdriver", \
"camping stuff" as a tent and sleeping bags, "Dad's things" as items whose details mention Dad. \
Speech-to-text and typing mistakes are common too.

Rules:
- Return every item that could be what they're after. The same kind of thing is often spread over \
several boxes; include all of them.
- Use the item's name, details, tags, box and place. Match on meaning, not just words.
- "why" is a few words saying how it matches, e.g. "same thing, different name" or "camping gear".
- confidence: "high" if it's clearly what they mean, "medium" if likely, "low" if it's a long shot \
worth checking. Leave out things that don't fit at all.
- Use only ids from the inventory. If nothing fits, return no matches and say so in "note"."""

SEARCH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["matches", "note"],
    "properties": {
        "matches": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "why", "confidence"],
            "properties": {
                "id": {"type": "string"},
                "why": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
        }},
        "note": {"type": "string"},
    },
}


def catalogue_text(catalogue: list[dict]) -> tuple[str, dict[str, dict]]:
    """One short line per item, in a fixed order so the prompt cache keeps working."""
    rows = sorted(catalogue, key=lambda i: (i.get("place", ""), i["boxName"], i["name"].lower(), i["id"]))
    lines, ids = [], {}
    for n, item in enumerate(rows, 1):
        key = f"i{n}"
        ids[key] = item
        qty = item.get("quantity") or 1
        box = f"{item['boxName']} ({item['place']})" if item.get("place") else item["boxName"]
        line = f"{key} | {box} | {item['name']} x{qty:g}"
        if item.get("description"):
            line += f" | {item['description']}"
        if item.get("tags"):
            line += f" | tags: {', '.join(item['tags'])}"
        lines.append(line.replace("\n", " "))
    return "\n".join(lines), ids


def claude_search(client: anthropic.Anthropic, model: str, query: str, catalogue: list[dict]) -> dict:
    """Ask Claude which items match a description. Returns {"matches": [item + why/confidence], "note", "usage"}."""
    if not catalogue:
        return {"matches": [], "note": "Nothing has been inventoried yet.", "usage": None}
    text, ids = catalogue_text(catalogue)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=8000,
            system=[
                {"type": "text", "text": SEARCH_PROMPT},
                # The inventory is the same for every search until something changes, so cache it.
                {"type": "text", "text": "Inventory (id | box (place) | item x quantity | details | tags):\n" + text,
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": f"What I'm looking for: {query.strip()}"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SEARCH_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.RateLimitError as exc:
        raise AnalysisError("Claude is rate limiting requests. Wait a moment and try again.") from exc
    except anthropic.APIStatusError as exc:
        raise AnalysisError(f"Claude API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AnalysisError("Couldn't reach the Claude API. Check the server's internet connection.") from exc

    out_text = next((b.text for b in response.content if b.type == "text"), None)
    if response.stop_reason == "refusal" or not out_text:
        raise AnalysisError("Claude couldn't do this search. Try different words.")
    try:
        out = json.loads(out_text)
    except json.JSONDecodeError as exc:
        raise AnalysisError("Claude returned something that wasn't valid JSON.") from exc

    order = {"high": 0, "medium": 1, "low": 2}
    seen, matches = set(), []
    for m in out.get("matches", []):
        item = ids.get(str(m.get("id", "")).strip())
        if item and item["id"] not in seen:
            seen.add(item["id"])
            matches.append({**item, "why": str(m.get("why") or ""), "confidence": m.get("confidence", "low")})
    matches.sort(key=lambda m: order.get(m["confidence"], 3))
    usage = response.usage
    return {
        "matches": matches,
        "note": str(out.get("note") or ""),
        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                  "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                  "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None)},
    }
