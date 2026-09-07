"""Pytest defaults for Keel — isolate from developer .env secrets."""
from __future__ import annotations

import os

# Force-skip repo .env for the whole suite (developer live keys must not leak in).
os.environ["KEEL_SKIP_DOTENV"] = "1"
