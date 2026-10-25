"""Settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    # How a box number becomes a location name. {n} is the number; numeric
    # numbers are zero-padded to BOX_NUMBER_DIGITS digits.
    box_name_format: str = field(default_factory=lambda: os.environ.get("BOX_NAME_FORMAT", "Box {n}"))
    box_number_digits: int = field(default_factory=lambda: int(os.environ.get("BOX_NUMBER_DIGITS", "3")))

    # Claude
    claude_model: str = field(default_factory=lambda: os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: os.environ.get("CLAUDE_EFFORT", "medium"))

    # Optional password for this app's web page (strongly recommended if it is
    # reachable from outside your home network).
    app_password: str = field(default_factory=lambda: os.environ.get("APP_PASSWORD", ""))

    # Save the spoken description into the box's notes in Homebox.
    save_transcript: bool = field(default_factory=lambda: _bool("SAVE_TRANSCRIPT", True))

    def box_name(self, number: str) -> str:
        number = number.strip()
        if number.isdigit():
            number = number.zfill(self.box_number_digits)
        return self.box_name_format.format(n=number)


settings = Settings()
