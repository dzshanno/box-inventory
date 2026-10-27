"""Settings, read from environment variables (see .env.example)."""

import os
import re
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _colours(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse "Red, Blue, Green=#2e7d32" into (("Red", "red"), ("Blue", "blue"), ("Green", "#2e7d32"))."""
    out = []
    for part in raw.split(","):
        name, _, css = part.partition("=")
        name = name.strip()
        if name:
            out.append((name, css.strip() or name.lower()))
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    # Where Homebox lives, as seen from this container.
    homebox_url: str = field(default_factory=lambda: os.environ.get("HOMEBOX_URL", "http://homebox:7745").rstrip("/"))
    # Homebox address as your phone sees it, used for "open in Homebox" links. Optional.
    homebox_public_url: str = field(default_factory=lambda: os.environ.get("HOMEBOX_PUBLIC_URL", "").rstrip("/"))
    # Either an API key (Profile -> API keys in Homebox) or a username/password.
    homebox_api_key: str = field(default_factory=lambda: os.environ.get("HOMEBOX_API_KEY", ""))
    homebox_username: str = field(default_factory=lambda: os.environ.get("HOMEBOX_USERNAME", ""))
    homebox_password: str = field(default_factory=lambda: os.environ.get("HOMEBOX_PASSWORD", ""))

    # All boxes are created inside this location. Leave empty to put boxes at the top level.
    parent_location: str = field(default_factory=lambda: os.environ.get("PARENT_LOCATION", "Storage Unit"))
    # Label colours in use. The same number in two colours is two different boxes.
    # Optionally give a screen colour: "Red, Blue, Green=#2e7d32". Empty = no colours.
    box_colours: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: _colours(os.environ.get("BOX_COLOURS", "Red, Blue")))
    # How a box becomes a location name. {colour} is the label colour, {n} the
    # number; numeric numbers are zero-padded to BOX_NUMBER_DIGITS digits.
    box_name_format: str = field(default_factory=lambda: os.environ.get("BOX_NAME_FORMAT", "Box {colour} {n}"))
    box_number_digits: int = field(default_factory=lambda: int(os.environ.get("BOX_NUMBER_DIGITS", "3")))

    # Claude
    claude_model: str = field(default_factory=lambda: os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: os.environ.get("CLAUDE_EFFORT", "medium"))

    # Optional password for this app's web page (strongly recommended if it is
    # reachable from outside your home network).
    app_password: str = field(default_factory=lambda: os.environ.get("APP_PASSWORD", ""))

    # Save the spoken description into the box's notes in Homebox.
    save_transcript: bool = field(default_factory=lambda: _bool("SAVE_TRANSCRIPT", True))

    # Boxes without a label yet are called "Unlabelled 001", "Unlabelled 002"…
    unlabelled_prefix: str = field(default_factory=lambda: os.environ.get("UNLABELLED_NAME", "Unlabelled"))

    def unlabelled_name(self, n: int) -> str:
        return f"{self.unlabelled_prefix} {str(n).zfill(self.box_number_digits)}"

    def unlabelled_number(self, name: str) -> int | None:
        """3 for "Unlabelled 003", None for anything that isn't an unlabelled box name."""
        m = re.fullmatch(re.escape(self.unlabelled_prefix) + r"\s+(\d+)", name.strip(), re.IGNORECASE)
        return int(m.group(1)) if m else None

    def __post_init__(self):
        if len(self.box_colours) > 1 and "{colour}" not in self.box_name_format:
            raise ValueError("BOX_NAME_FORMAT must include {colour} when BOX_COLOURS lists more than one colour, "
                             "or red and blue boxes with the same number would be mixed up.")

    def colour(self, value: str) -> str:
        """Return the configured spelling of a colour, or raise ValueError if it isn't configured."""
        if not self.box_colours:
            return ""
        for name, _ in self.box_colours:
            if name.lower() == value.strip().lower():
                return name
        names = ", ".join(n for n, _ in self.box_colours)
        raise ValueError(f"Pick a label colour ({names})." if not value.strip()
                         else f"Unknown label colour '{value.strip()}'. Use one of: {names}.")

    def box_name(self, number: str, colour: str = "") -> str:
        number = number.strip()
        if number.isdigit():
            # "42", "042" and "0042" are all the same box
            number = str(int(number)).zfill(self.box_number_digits)
        name = self.box_name_format.format(n=number, colour=self.colour(colour))
        return re.sub(r"\s+", " ", name).strip()


settings = Settings()
