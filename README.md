# Box Inventory for Homebox

Inventory a storage box from your phone in about a minute:

1. Photograph the box's label. Claude reads the colour and number; check them and tap **Start**. (Or type them in.)
2. Photograph the open box.
3. Tap the mic and say what's inside.
4. Claude turns the photos and your description into an item list.
5. Fix anything that's wrong, then tap **Save to Homebox**.

In Homebox, each box becomes a location inside the place it's kept (for example `Workshop › Box Red 042`). The label colour is part of the box's identity, so red 042 and blue 042 are two different boxes. Everything in it becomes an item inside that location, with tags and quantities. The photos are attached to the box, and your spoken description is kept in the box's notes. A search for "boots" in Homebox then tells you which box to fetch.

**No label yet?** Tap **This box has no label yet** (or it's offered when Claude finds no label). The box is saved as `Unlabelled 001`, `Unlabelled 002` and so on. Once you've stuck a label on, tap **Label an unlabelled box**, pick it from the list (it shows what's inside), and photograph or type the new label. The box is renamed and keeps everything in it. You can't give a box a label that another box already has.

## The home screen

- **Add a box**: the steps above. First pick **Where is it?**: one of your places, a room from Home Assistant, or **+ Add a new place…**. A box label is unique everywhere, so adding to a box from a different place moves it there.
- **Change a box**: photograph or type a label (or pick an unlabelled box) to see what's in it, where it is and when it was last seen. Tap an item to fix its name, quantity, details or tags, or delete it. **Add items** adds more, **Place** moves it, and **Change label** renames the box.
- **Find something**: type what you're after. Matches show as you type, and typos, plurals and half-typed words are fine ("chrismas lite", "drils"). Box names work too ("red 47"). If nothing uses your words, Claude reads the whole inventory and finds things stored under other words: "the christmas decorations" finds baubles, tinsel and a nativity set across several boxes. You can also tap **Search by meaning** yourself. When the results are in several boxes, **Spot these boxes in a photo** highlights each one in a wide photo of the lockup.

### Places, last seen and the phone's location

Each box records when it was **last seen**, where, and how: photographed, label scanned, spotted in a photo (with the spot, like "back-right shelf, bottom row") or moved. Search results and the box page show it. It's a "Last seen" field on the box in Homebox.

Places come from Homebox's top-level locations (or the ones inside `STORAGE_ROOT`). Set `HA_URL` and `HA_TOKEN` to offer your Home Assistant areas too; picking one creates the place.

The app remembers the last place you used. If the page is on HTTPS and you allow location, it also learns where each place is, so at the lockup it picks Lockup on its own. GPS can't tell rooms apart, so at home it picks the last room you used there. The phone's location stays on the phone. Photo location (EXIF) isn't used: phones usually strip it from uploads, and the app shrinks photos before sending them anyway.

Opening a box that already exists shows what's recorded. Claude is told about those items and only suggests new ones, so you can add to a box later.

Works with **Homebox v0.27 or later**, which uses the "entities" and "tags" API.

## Setup (Docker)

You need Docker with Compose on your home server, plus an Anthropic API key from <https://platform.claude.com>.

```bash
git clone <this repo> box-inventory && cd box-inventory
cp .env.example .env
```

### If you don't have Homebox yet

`docker-compose.yml` runs Homebox and Box Inventory together.

1. In `.env`, set `HBOX_AUTH_API_KEY_PEPPER` to the output of `openssl rand -base64 48`, and set `ANTHROPIC_API_KEY`.
2. Start Homebox: `docker compose up -d homebox`.
3. Open `http://<server>:3100`, register your account, and create a collection if asked.
4. In Homebox, go to **Profile → API keys** and create a key. Put it in `.env` as `HOMEBOX_API_KEY`. New keys expire after 30 days by default, so choose a longer expiry, or use `HOMEBOX_USERNAME` and `HOMEBOX_PASSWORD` instead.
5. Start everything: `docker compose up -d --build`.
6. Open `http://<server>:8080`.

### If Homebox is already running

Delete the `homebox` service from `docker-compose.yml`. Set `HOMEBOX_URL` in `.env` to your Homebox address as seen from the container, for example `http://192.168.1.10:3100`. Then follow steps 4–6 above.

## Using it away from home (and why HTTPS matters)

Phone browsers only allow the microphone on **HTTPS** pages. Over plain `http://` the mic button is hidden, but you can still dictate with your keyboard's microphone key.

To reach the app from the storage unit with HTTPS, the easiest option is **[Tailscale](https://tailscale.com)**:

1. Install Tailscale on the server and on your phone.
2. On the server, run `tailscale serve --bg 8080`.
3. On your phone, open the `https://<server-name>.<tailnet>.ts.net` address it prints.

Nothing is exposed to the public internet, and you get a valid certificate. If you already run a reverse proxy (Caddy, Traefik, Nginx Proxy Manager, Cloudflare Tunnel), point it at port 8080 instead.

**Set `APP_PASSWORD`** if the page can be reached from anywhere other than your home network. Anyone who can open the page can add items to your Homebox and spend your Claude credit.

## Tips for good results

- Take two or three photos: the label, the open box from above, and one with the top layer moved aside. Claude reads brand names and counts things it can see.
- Say what the photos can't show: "underneath there's a set of Le Creuset pans, the blue ones, and the kids' school reports from 2015 to 2019".
- Rough counts are fine ("loads of", "about twenty"). Claude estimates and says so in the item's details.
- Items marked **unsure, please check** were only guessed from a photo.
- After saving, **Next box** moves straight on to the next number.

## Cost

Each box is one Claude request (`claude-opus-5-5` by default), plus a small one when you photograph the label. A search by meaning sends the whole inventory list to Claude; it's cached for 5 minutes, so the first search costs a few cents for a full lockup and the next ones well under a cent. Set `SEARCH_MODEL=claude-haiku-5-5` to make searches much cheaper. Typing in the search box never calls Claude. With a few photos that's typically a few cents, so all 120 boxes come to a few dollars. `CLAUDE_EFFORT=low` is cheaper and faster, and `high` is more thorough. The server log records the token count for each box.

## Settings

All settings go in `.env`. See `.env.example` for the full list. The main ones:

| Setting | Default | What it does |
| --- | --- | --- |
| `STORAGE_ROOT` | (empty) | Homebox location your places are inside, e.g. `Home`. Empty means places are top-level locations. |
| `DEFAULT_PLACE` | `Storage Unit` | Place for new boxes until you pick one. Created if missing. (`PARENT_LOCATION` still works.) |
| `HA_URL`, `HA_TOKEN` | (empty) | Home Assistant address and long-lived token, to offer its areas as places. |
| `BOX_COLOURS` | `Red, Blue` | Label colours you use. Add a screen colour with `Green=#2e7d32`. Empty turns colours off. |
| `BOX_NAME_FORMAT` | `Box {colour} {n}` | How a box becomes a name. Must include `{colour}` if you have more than one colour. |
| `BOX_NUMBER_DIGITS` | `3` | Zero-padding for numbers: red `42` becomes `Box Red 042`. |
| `UNLABELLED_NAME` | `Unlabelled` | Name for boxes without a label: `Unlabelled 001`. |
| `SAVE_TRANSCRIPT` | `true` | Store your spoken description in the box's notes. |
| `HOMEBOX_PUBLIC_URL` | (empty) | Adds an "Open in Homebox" link after saving. |
| `CLAUDE_EFFORT` | `medium` | `low`, `medium` or `high`. |
| `SEARCH_MODEL` | (empty) | Model for search by meaning. Empty uses `CLAUDE_MODEL`. |

## Development

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
```

The tests run the whole flow against a real, disposable Homebox, with Claude replaced by a stand-in:

```bash
HOMEBOX_TEST_URL=http://localhost:7745 HOMEBOX_TEST_API_KEY=hb_... pytest
```

Code layout:

- `app/main.py`: web routes (boxes, labels, items, search, photo finder, analyse and save)
- `app/search.py`: quick word matching and Claude search
- `app/homeassistant.py`: reads area names from Home Assistant
- `app/analyze.py`: the Claude prompts (label reading and inventory) and output formats
- `app/homebox.py`: Homebox API client
- `app/static/`: the phone page
