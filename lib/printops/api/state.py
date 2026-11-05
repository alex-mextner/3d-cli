"""state.py — per-app runtime state of 3d-api (`app.state.printops`)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from printops.settings import Settings
from printops.spool_mirror import SyncedSpoolman
from printops.spoolman import Spoolman
from printops.tokens import TokenStore


@dataclass
class ApiState:
    settings: Settings
    # The plain Spoolman client: for reads and extra-field registration only. Domain code that
    # WRITES spools gets `synced(conn)` (the one write path, spool_mirror.py) —
    # tests/test_printops_spool_sync_architecture.py lists who may touch this attribute.
    spoolman: Spoolman | None
    tokens: TokenStore
    # Outcome of the background extra-field registration (see app._keep_fields_registered).
    fields: dict[str, Any] = field(
        default_factory=lambda: {"status": "pending", "error": None}
    )

    def synced(self, conn: sqlite3.Connection) -> SyncedSpoolman | None:
        """Spoolman through the write path that keeps PrintOps' bookmark of every spool in step
        (None when no Spoolman is configured)."""
        if self.spoolman is None:
            return None
        return SyncedSpoolman(self.spoolman, conn)
