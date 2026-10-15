"""state.py — per-app runtime state of 3d-api (`app.state.printops`)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from printops.settings import Settings
from printops.spoolman import Spoolman
from printops.tokens import TokenStore


@dataclass
class ApiState:
    settings: Settings
    spoolman: Spoolman | None
    tokens: TokenStore
    # Outcome of the background extra-field registration (see app._keep_fields_registered).
    fields: dict[str, Any] = field(default_factory=lambda: {"status": "pending", "error": None})
