"""FastAPI Dashboard & Desktop API Router for `hermes-reflex`.

Mounted automatically by Hermes Web/Desktop gateway to serve:
- Widget specifications for `::reflex-ui{id="..."}` transcript directives
- Sticky-note sidepane pin/unpin/note persistence (`PANES_AREA`)
- Live skill execution when users manipulate sliders/buttons inside a widget
- Reflex compilation & Curator `.archive/` rescue telemetry
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

try:
    from fastapi import APIRouter, Body, HTTPException
except ImportError:  # Graceful fallback for environments without FastAPI installed

    class APIRouter:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.routes = []

        def get(self, path: str, **kwargs: Any):
            def decorator(fn):
                self.routes.append(("GET", path, fn))
                return fn

            return decorator

        def post(self, path: str, **kwargs: Any):
            def decorator(fn):
                self.routes.append(("POST", path, fn))
                return fn

            return decorator

    def Body(default: Any = None, **kwargs: Any) -> Any:  # type: ignore[misc]
        return default

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail


try:
    from .. import get_active_plugin
except (ImportError, ValueError):
    try:
        from hermes_reflex import get_active_plugin
    except (ImportError, ValueError):
        import importlib
        get_active_plugin = importlib.import_module("hermes-reflex").get_active_plugin

router = APIRouter(prefix="/api/plugins/hermes-reflex", tags=["hermes-reflex"])


@router.get("/status")
def get_reflex_status() -> Dict[str, Any]:
    plugin = get_active_plugin()
    skills = plugin.store.list_compiled_skills()
    pinned = plugin.store.list_widgets(pinned_only=True)
    archive_events = plugin.store.list_archive_events(limit=20)
    return {
        "ok": True,
        "plugin": "hermes-reflex",
        "version": "1.0.0",
        "compiled_skills_count": len(skills),
        "trusted_skills_count": sum(1 for s in skills if s.get("status") == "TRUSTED"),
        "tested_skills_count": sum(1 for s in skills if s.get("status") == "TESTED"),
        "pinned_widgets_count": len(pinned),
        "compiled_skills": skills,
        "pinned_widgets": pinned,
        "recent_archive_events": archive_events,
    }


@router.get("/widgets/{widget_id}")
def get_widget_by_id(widget_id: str) -> Dict[str, Any]:
    plugin = get_active_plugin()
    widget = plugin.store.get_widget(widget_id)
    if not widget:
        raise HTTPException(status_code=404, detail=f"Widget '{widget_id}' not found")
    return {"ok": True, "widget": widget}


@router.get("/widgets")
def list_widgets(session_id: Optional[str] = None, pinned_only: bool = False) -> Dict[str, Any]:
    plugin = get_active_plugin()
    widgets = plugin.store.list_widgets(session_id=session_id, pinned_only=pinned_only)
    return {"ok": True, "widgets": widgets}


@router.post("/widgets/{widget_id}/pin")
def update_widget_pin(widget_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    plugin = get_active_plugin()
    pinned = bool(payload.get("pinned", True))
    note_color = payload.get("note_color")
    user_notes = payload.get("user_notes")
    pin_order = payload.get("pin_order")

    updated = plugin.store.set_widget_pin(
        widget_id=widget_id,
        pinned=pinned,
        note_color=note_color,
        user_notes=user_notes,
        pin_order=pin_order,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Widget '{widget_id}' not found")
    return {"ok": True, "widget": updated}


@router.post("/widgets/{widget_id}/controls")
def update_widget_controls(widget_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """Persist live slider/toggle adjustments made inside a GenUI card or Sticky-Note Sidepane."""
    plugin = get_active_plugin()
    widget = plugin.store.get_widget(widget_id)
    if not widget:
        raise HTTPException(status_code=404, detail=f"Widget '{widget_id}' not found")

    spec = dict(widget.get("spec") or {})
    control_values = dict(payload.get("values") or {})
    for ctrl in spec.get("controls", []):
        key = ctrl.get("key")
        if key in control_values:
            ctrl["default_value"] = control_values[key]
    spec["last_interactive_values"] = control_values
    widget["spec"] = spec
    if "user_notes" in payload:
        widget["user_notes"] = str(payload["user_notes"])
    if "note_color" in payload:
        widget["note_color"] = str(payload["note_color"])

    saved = plugin.store.upsert_widget(widget)
    return {"ok": True, "widget": saved}


@router.post("/skills/compile")
def compile_skill_endpoint(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    plugin = get_active_plugin()
    raw = plugin.tool_compile_skill(
        skill_name=str(payload.get("skill_name", "")),
        compile_all=bool(payload.get("compile_all", False)),
        force=bool(payload.get("force", False)),
    )
    return json.loads(raw)


@router.post("/skills/run")
def run_skill_endpoint(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    plugin = get_active_plugin()
    raw = plugin.tool_run_skill(
        skill_name=str(payload.get("skill_name", "")),
        params_json=json.dumps(payload.get("params", {})),
        dry_run=bool(payload.get("dry_run", False)),
        session_id=str(payload.get("session_id", "")),
    )
    return json.loads(raw)


@router.post("/archive/audit")
def audit_archive_endpoint(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    plugin = get_active_plugin()
    raw = plugin.tool_audit_archive(
        auto_rescue=bool(payload.get("auto_rescue", True)),
        skill_name=str(payload.get("skill_name", "")),
    )
    return json.loads(raw)
