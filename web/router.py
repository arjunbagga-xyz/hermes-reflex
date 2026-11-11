"""Web router for hermes-reflex."""
from __future__ import annotations

import importlib
from typing import Any

try:
    from ..dashboard.plugin_api import router
except (ImportError, ValueError):
    try:
        from dashboard.plugin_api import router
    except (ImportError, ValueError):
        router = importlib.import_module("hermes-reflex.dashboard.plugin_api").router
