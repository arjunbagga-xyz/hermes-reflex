"""Hermes Agent Plugin Entrypoint (`hermes-reflex`).

Strictly adheres to Hermes v0.23.1 plugin contracts:
1. Auxiliary tasks (`reflex_compiler`, `reflex_genui`) registered via `ctx.register_auxiliary_task`.
2. Tool handlers ALWAYS return `json.dumps(...) -> str` (never raw Python `dict`).
3. Hook handlers (`pre_llm_call`, `transform_llm_output`, `on_skill_lifecycle`, `post_curator_run`)
   use exact keyword parameters + `**kwargs` and return exact Hermes types (`dict | None` for
   `pre_llm_call`, `str | None` for `transform_llm_output`, `None` for lifecycle/curator observers).
4. Transcript directives use strict `::reflex-ui{id="..." kind="..." title="..." pinned="..."}` syntax.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .archive_healer import ArchiveHealer
    from .compiler import ReflexSkillCompiler
    from .genui import GenUIEngine, format_reflex_directive
    from .store import ReflexStore
except (ImportError, ValueError):
    from archive_healer import ArchiveHealer
    from compiler import ReflexSkillCompiler
    from genui import GenUIEngine, format_reflex_directive
    from store import ReflexStore

_ACTIVE_INSTANCE: Optional["HermesReflexPlugin"] = None


def get_active_plugin() -> "HermesReflexPlugin":
    global _ACTIVE_INSTANCE
    if _ACTIVE_INSTANCE is None:
        _ACTIVE_INSTANCE = HermesReflexPlugin()
    return _ACTIVE_INSTANCE


class HermesReflexPlugin:
    """Unified coordinator for Skill-to-Code Reflexes, Archive Healing, and Sticky-Note GenUI."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        skills_root: Optional[Path | str] = None,
        config: Optional[Dict[str, Any]] = None,
        ctx: Any = None,
    ) -> None:
        self.ctx = ctx
        self.config: Dict[str, Any] = dict(config or {})
        self.store = ReflexStore(db_path=db_path)
        self.compiler = ReflexSkillCompiler(
            store=self.store,
            skills_root=skills_root,
            config=self.config,
        )
        self.archive_healer = ArchiveHealer(
            store=self.store,
            compiler=self.compiler,
        )
        self.genui = GenUIEngine(
            store=self.store,
            config=self.config,
        )

    # ------------------------------------------------------------------
    # Tool Handlers (MUST return `str` via `json.dumps`, NEVER raw `dict`)
    # ------------------------------------------------------------------

    def tool_compile_skill(
        self,
        skill_name: str = "",
        compile_all: bool = False,
        force: bool = False,
        **kwargs: Any,
    ) -> str:
        """Compile one or all saved Hermes skills into verified executable Python reflexes."""
        if compile_all or not str(skill_name).strip():
            skills = self.compiler.discover_skills(include_archived=False)
            results = []
            for item in skills:
                res = self.compiler.compile_skill(
                    item["skill_name"],
                    ctx=self.ctx,
                    force=bool(force),
                )
                results.append(res)
            return json.dumps(
                {
                    "ok": True,
                    "mode": "batch_compile",
                    "total_skills": len(results),
                    "compiled_ok": sum(1 for r in results if r.get("ok")),
                    "results": results,
                }
            )

        res = self.compiler.compile_skill(
            str(skill_name).strip(),
            ctx=self.ctx,
            force=bool(force),
        )
        return json.dumps(res)

    def tool_run_skill(
        self,
        skill_name: str,
        params_json: str = "{}",
        dry_run: bool = False,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        """Execute a compiled Hermes skill with injected runtime parameters and automatic Reverse Escalation."""
        try:
            params = json.loads(params_json) if isinstance(params_json, str) and params_json.strip() else {}
            if not isinstance(params, dict):
                params = {}
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "status": "ESCALATE",
                    "skill_name": skill_name,
                    "error": f"Invalid params_json: {exc}",
                }
            )

        res = self.compiler.execute_skill(
            skill_name=str(skill_name).strip(),
            params=params,
            session_id=str(session_id or ""),
            dry_run=bool(dry_run),
            ctx=self.ctx,
        )
        return json.dumps(res)

    def tool_audit_archive(
        self,
        auto_rescue: bool = True,
        skill_name: str = "",
        **kwargs: Any,
    ) -> str:
        """Audit `.archive/` skills and rescue high-value procedural skills from the Curator black hole."""
        if str(skill_name).strip():
            res = self.archive_healer.rescue_skill(
                skill_name=str(skill_name).strip(),
                compile_after_rescue=True,
                ctx=self.ctx,
            )
            return json.dumps(res)

        res = self.archive_healer.audit_and_heal_all(
            auto_rescue=bool(auto_rescue),
            compile_rescued=True,
            ctx=self.ctx,
        )
        return json.dumps(res)

    def tool_pin_widget(
        self,
        action: str = "pin",
        widget_id: str = "",
        kind: str = "chart",
        title: str = "Custom Widget",
        spec_json: str = "{}",
        note_color: str = "amber",
        user_notes: str = "",
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        """Create, pin, unpin, or list GenUI widgets on the permanent Sticky-Note Sidepane."""
        act = str(action or "pin").strip().lower()
        if act == "list":
            widgets = self.store.list_widgets(session_id=session_id or None, pinned_only=True)
            return json.dumps({"ok": True, "action": "list", "pinned_widgets": widgets})

        if act == "unpin" and widget_id:
            updated = self.store.set_widget_pin(widget_id, pinned=False)
            return json.dumps({"ok": bool(updated), "action": "unpin", "widget": updated})

        if act == "create":
            wid = widget_id or f"w_{uuid.uuid4().hex[:10]}"
            try:
                spec = json.loads(spec_json) if spec_json else {}
            except Exception:
                spec = {}
            saved = self.store.upsert_widget(
                {
                    "widget_id": wid,
                    "session_id": session_id,
                    "kind": kind,
                    "title": title,
                    "subtitle": "Created via reflex_pin_widget",
                    "spec": spec,
                    "pinned": True,
                    "note_color": note_color,
                    "user_notes": user_notes,
                }
            )
            directive = format_reflex_directive(wid, kind, title, pinned=True)
            return json.dumps(
                {
                    "ok": True,
                    "action": "create",
                    "widget": saved,
                    "directive": directive,
                }
            )

        # Default: pin existing widget
        updated = self.store.set_widget_pin(
            widget_id=widget_id,
            pinned=True,
            note_color=note_color or None,
            user_notes=user_notes if user_notes != "" else None,
        )
        if not updated:
            return json.dumps({"ok": False, "error": f"Widget '{widget_id}' not found."})
        return json.dumps({"ok": True, "action": "pin", "widget": updated})

    # ------------------------------------------------------------------
    # Event Hooks (Strict Hermes v0.23.1 Keyword Signatures & Return Types)
    # ------------------------------------------------------------------

    def hook_pre_llm_call(
        self,
        session_id: str = "",
        user_message: str = "",
        conversation_history: Optional[List[ Any]] = None,
        is_first_turn: bool = False,
        model: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Inject available verified executable skill runners into the turn context when relevant."""
        matches = self.compiler.match_skills_for_user_message(user_message, ctx=self.ctx)
        if not matches:
            return None

        lines = [
            "[Hermes Reflex Executable Skills Available]",
            "The following saved skills have verified executable code runners (`reflex_run_skill`) "
            "that coexist with their prose SKILL.md instructions:",
        ]
        for m in matches:
            schema_props = list((m.get("parameters_schema") or {}).get("properties", {}).keys())
            lines.append(
                f"- `{m['skill_name']}` (status={m['status']}, bayesian_score={m['bayesian_score']}, "
                f"params={schema_props}, defaults={m.get('default_params', {})})"
            )
        lines.append(
            "Prefer calling `reflex_run_skill` with `params_json` for fast, deterministic execution. "
            "If `reflex_run_skill` returns `status == 'ESCALATE'`, fall back to the prose SKILL.md steps."
        )
        return {"context": "\n".join(lines)}

    def hook_transform_llm_output(
        self,
        response_text: str = "",
        session_id: str = "",
        model: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> Optional[str]:
        """Run the Two-Tier GenUI pass on the final assistant output and return an upgraded string."""
        return self.genui.transform_output(
            response_text=response_text,
            session_id=session_id,
            ctx=self.ctx,
        )

    def hook_on_skill_lifecycle(
        self,
        action: str = "",
        skill_name: str = "",
        provenance: str = "",
        task_id: str = "",
        session_id: str = "",
        use_count: int = 0,
        reused: bool = False,
        reuse_after_patch: bool = False,
        **kwargs: Any,
    ) -> None:
        """Observer hook for skill lifecycle mutations, live patches, and archive black-hole rescue."""
        act = str(action or "").lower()
        name = str(skill_name or "").strip()
        if not name:
            return None

        if act in ("archived", "stale") and bool(self.config.get("auto_rescue_archived_skills", True)):
            self.archive_healer.rescue_skill(name, compile_after_rescue=True, ctx=self.ctx)
            return None

        if (
            reuse_after_patch
            or act in ("created", "patched", "updated", "restored")
            and bool(self.config.get("auto_compile_on_patch", True))
        ):
            self.compiler.compile_skill(name, ctx=self.ctx, force=True)
        return None

    def hook_post_curator_run(
        self,
        run_dir: str = "",
        run_json_path: str = "",
        report_md_path: str = "",
        dry_run: bool = False,
        **kwargs: Any,
    ) -> None:
        """Observer hook after Curator runs: rescues cold-start skills moved to `.archive/`."""
        if dry_run:
            return None
        if bool(self.config.get("auto_rescue_archived_skills", True)):
            self.archive_healer.audit_and_heal_all(
                auto_rescue=True,
                compile_rescued=True,
                ctx=self.ctx,
            )
        return None


def register(ctx: Any) -> None:
    """Official Hermes Plugin Entrypoint."""
    global _ACTIVE_INSTANCE

    plugin_cfg: Dict[str, Any] = {}
    if hasattr(ctx, "get_config"):
        try:
            plugin_cfg = dict(ctx.get_config() or {})
        except Exception:
            plugin_cfg = {}
    elif hasattr(ctx, "config") and isinstance(ctx.config, dict):
        plugin_cfg = dict(ctx.config)

    # Register Auxiliary LLM Tasks (`ctx.register_auxiliary_task`) so `ctx.llm` calls work cleanly
    if hasattr(ctx, "register_auxiliary_task"):
        try:
            ctx.register_auxiliary_task(
                "reflex_compiler",
                display_name="Reflex Skill Compiler",
                description="Extracts parameter schemas, generates executable skill code, and synthesizes dry-run verification tests.",
                inherit_from="compression",
                defaults={"timeout": 90},
            )
            ctx.register_auxiliary_task(
                "reflex_genui",
                display_name="Reflex GenUI Visual Synthesizer",
                description="Transforms prose-heavy final outputs into native Hermes charts, diagrams, and interactive control widgets.",
                inherit_from="compression",
                defaults={"timeout": 45},
            )
        except Exception:
            pass

    plugin = HermesReflexPlugin(config=plugin_cfg, ctx=ctx)
    _ACTIVE_INSTANCE = plugin

    # Register 4 Tools
    ctx.register_tool(
        name="reflex_compile_skill",
        toolset="hermes-reflex",
        schema={
            "name": "reflex_compile_skill",
            "description": (
                "Compile a saved Hermes SKILL.md into a parameterized executable Python runner "
                "(${HERMES_SKILL_DIR}/scripts/reflex_runner.py) and run 3-gate pre-trust verification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to compile (leave empty if compile_all=true).",
                    },
                    "compile_all": {
                        "type": "boolean",
                        "description": "Compile and verify all discovered skills in ~/.hermes/skills.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Force re-compilation even if SKILL.md dir_hash is unchanged.",
                    },
                },
                "required": [],
            },
        },
        handler=plugin.tool_compile_skill,
    )

    ctx.register_tool(
        name="reflex_run_skill",
        toolset="hermes-reflex",
        schema={
            "name": "reflex_run_skill",
            "description": (
                "Execute a compiled Hermes skill runner with injected JSON parameters. "
                "Automatically falls back ('ESCALATE') to prose SKILL.md if preconditions fail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the compiled skill to execute.",
                    },
                    "params_json": {
                        "type": "string",
                        "description": "JSON-encoded dictionary of runtime parameters to inject.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Validate parameter injection and resolved commands without side effects.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional active session ID for telemetry.",
                    },
                },
                "required": ["skill_name"],
            },
        },
        handler=plugin.tool_run_skill,
    )

    ctx.register_tool(
        name="reflex_audit_archive",
        toolset="hermes-reflex",
        schema={
            "name": "reflex_audit_archive",
            "description": (
                "Scan `<skills_root>/.archive/` and rescue high-value procedural skills from the "
                "Curator archive black hole using Bayesian Laplace smoothing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "auto_rescue": {
                        "type": "boolean",
                        "description": "Automatically restore qualifying skills to active categories and compile them.",
                    },
                    "skill_name": {
                        "type": "string",
                        "description": "Optional specific skill name in .archive/ to rescue immediately.",
                    },
                },
                "required": [],
            },
        },
        handler=plugin.tool_audit_archive,
    )

    ctx.register_tool(
        name="reflex_pin_widget",
        toolset="hermes-reflex",
        schema={
            "name": "reflex_pin_widget",
            "description": (
                "Pin, unpin, create, or list interactive GenUI widgets on the permanent Sticky-Note Sidepane."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pin", "unpin", "create", "list"],
                        "description": "Pinboard action to perform.",
                    },
                    "widget_id": {
                        "type": "string",
                        "description": "Target widget ID (e.g., w_a1b2c3d4e5).",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["chart", "flow", "controls", "metrics"],
                        "description": "Widget kind when action='create'.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Widget title when action='create'.",
                    },
                    "spec_json": {
                        "type": "string",
                        "description": "JSON widget specification when action='create'.",
                    },
                    "note_color": {
                        "type": "string",
                        "enum": ["amber", "emerald", "cyan", "violet"],
                        "description": "Sticky-note accent color tag in the sidepane.",
                    },
                    "user_notes": {
                        "type": "string",
                        "description": "Optional sticky note annotation attached to the pinned widget.",
                    },
                },
                "required": ["action"],
            },
        },
        handler=plugin.tool_pin_widget,
    )

    # Register 4 Lifecycle & Pipeline Hooks
    ctx.register_hook("pre_llm_call", plugin.hook_pre_llm_call)
    ctx.register_hook("transform_llm_output", plugin.hook_transform_llm_output)
    ctx.register_hook("on_skill_lifecycle", plugin.hook_on_skill_lifecycle)
    ctx.register_hook("post_curator_run", plugin.hook_post_curator_run)

    # Register CLI Slash Commands (`/reflex` and `/pinboard`) if supported by context
    if hasattr(ctx, "register_cli_command"):
        try:
            ctx.register_cli_command(
                name="reflex",
                description="Manage compiled executable skills and Curator archive recovery (/reflex status | compile | heal)",
                handler=lambda args="", **kw: plugin.tool_compile_skill(
                    compile_all=(str(args).strip() in ("compile", "all", "")),
                    skill_name=(str(args).replace("compile", "").strip() if "compile" in str(args) else ""),
                ),
            )
            ctx.register_cli_command(
                name="pinboard",
                description="List or manage pinned sticky-note GenUI widgets (/pinboard list)",
                handler=lambda args="", **kw: plugin.tool_pin_widget(action="list"),
            )
        except Exception:
            pass

    # Register Desktop RPC Methods if supported by context
    if hasattr(ctx, "register_rpc"):
        try:
            ctx.register_rpc("reflex.get_widget", lambda payload: plugin.store.get_widget(payload.get("widget_id", "")))
            ctx.register_rpc(
                "reflex.list_pinned",
                lambda payload: plugin.store.list_widgets(
                    session_id=payload.get("session_id"), pinned_only=True
                ),
            )
            ctx.register_rpc(
                "reflex.pin_widget",
                lambda payload: json.loads(
                    plugin.tool_pin_widget(
                        action=payload.get("action", "pin"),
                        widget_id=payload.get("widget_id", ""),
                        note_color=payload.get("note_color", "amber"),
                        user_notes=payload.get("user_notes", ""),
                    )
                ),
            )
            ctx.register_rpc(
                "reflex.run_skill",
                lambda payload: json.loads(
                    plugin.tool_run_skill(
                        skill_name=payload.get("skill_name", ""),
                        params_json=json.dumps(payload.get("params", {})),
                        dry_run=bool(payload.get("dry_run", False)),
                    )
                ),
            )
        except Exception:
            pass
