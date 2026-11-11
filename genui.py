"""Two-Tier Final-Output GenUI Engine for Hermes (`transform_llm_output`).

Takes the final assistant output text and evaluates whether any or all of it
is clearer as an interactive native Hermes GenUI widget card (`::reflex-ui{...}`):

- Tier 1 (Zero-Token Fast Path):
  Detects structured Markdown tables with numeric data, multi-step sequential workflows,
  or explicit parameter/metric blocks and synthesizes interactive chart, flow, or slider
  control cards with zero extra latency.
- Tier 2 (Single Final LLM Visual Synthesis Call):
  When `enable_genui_llm_pass` is true and the final response contains paragraphs of prose
  explaining quantitative trade-offs, comparisons, architectures, or tunable models, makes
  a single `ctx.llm.complete_structured(task="reflex_genui")` call to synthesize a rich
  visual representation (chart, flow diagram, or interactive slider/button calculator).

Every generated widget is stored in SQLite (`ReflexStore`) and emitted via a standalone
Hermes transcript directive:
  `::reflex-ui{id="<widget_id>" kind="<kind>" title="<escaped_title>" pinned="false"}`
so the Hermes Desktop UI renders it with `@hermes/plugin-sdk` components and allows dragging
it into the permanent Sticky-Note Sidepane (`PANES_AREA`).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

try:
    from .store import ReflexStore
except (ImportError, ValueError):
    from store import ReflexStore

GENUI_STRUCTURED_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "should_render_widget": {"type": "boolean"},
        "kind": {
            "type": "string",
            "enum": ["chart", "flow", "controls", "metrics"],
        },
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "chart_type": {
            "type": "string",
            "enum": ["bar", "line", "comparison"],
        },
        "data_points": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "number"},
                    "secondary_value": {"type": "number"},
                    "unit": {"type": "string"},
                },
                "required": ["label", "value", "secondary_value", "unit"],
            },
        },
        "flow_nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "detail": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["id", "label", "detail", "status"],
            },
        },
        "controls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string"},
                    "label": {"type": "string"},
                    "control_type": {
                        "type": "string",
                        "enum": ["slider", "toggle", "segment"],
                    },
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                    "step": {"type": "number"},
                    "default_value": {"type": "number"},
                    "unit": {"type": "string"},
                },
                "required": [
                    "key",
                    "label",
                    "control_type",
                    "min",
                    "max",
                    "step",
                    "default_value",
                    "unit",
                ],
            },
        },
        "output_metric_label": {"type": "string"},
        "output_metric_unit": {"type": "string"},
        "action_skill_name": {"type": "string"},
    },
    "required": [
        "should_render_widget",
        "kind",
        "title",
        "subtitle",
        "chart_type",
        "data_points",
        "flow_nodes",
        "controls",
        "output_metric_label",
        "output_metric_unit",
        "action_skill_name",
    ],
}


def sanitize_directive_attr(value: str) -> str:
    """Sanitize a string for safe inclusion inside `::reflex-ui{key="value"}`."""
    cleaned = re.sub(r'["\\\r\n{}]', " ", str(value or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:90] or "Widget"


def format_reflex_directive(
    widget_id: str,
    kind: str,
    title: str,
    pinned: bool = False,
) -> str:
    """Format a strict Hermes TRANSCRIPT_DIRECTIVE_AREA directive line."""
    safe_id = sanitize_directive_attr(widget_id)
    safe_kind = sanitize_directive_attr(kind)
    safe_title = sanitize_directive_attr(title)
    safe_pinned = "true" if pinned else "false"
    return f'::reflex-ui{{id="{safe_id}" kind="{safe_kind}" title="{safe_title}" pinned="{safe_pinned}"}}'


class GenUIEngine:
    """Synthesizes interactive Hermes GenUI widgets from final assistant output."""

    def __init__(self, store: ReflexStore, config: Optional[Dict[str, Any]] = None) -> None:
        self.store = store
        self.config = config or {}

    @staticmethod
    def _make_widget_id(session_id: str, seed: str) -> str:
        digest = hashlib.sha256(f"{session_id}:{seed}".encode("utf-8")).hexdigest()[:10]
        return f"w_{digest}"

    def _extract_markdown_table_widget(
        self, response_text: str, session_id: str
    ) -> Optional[Dict[str, Any]]:
        """Tier 1A: Parse Markdown pipe tables containing numeric metrics into interactive chart cards."""
        lines = response_text.splitlines()
        table_blocks: List[List[str]] = []
        current: List[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3:
                current.append(stripped)
            else:
                if len(current) >= 3:
                    table_blocks.append(current)
                current = []
        if len(current) >= 3:
            table_blocks.append(current)

        if not table_blocks:
            return None

        block = table_blocks[0]
        headers = [c.strip().strip("*_") for c in block[0].strip("|").split("|")]
        if len(headers) < 2:
            return None

        # Verify separator line
        if not re.match(r"^[\s|:-]+$", block[1]):
            return None

        data_points: List[Dict[str, Any]] = []
        rows: List[List[str]] = []
        for row_line in block[2:]:
            cells = [c.strip() for c in row_line.strip("|").split("|")]
            if len(cells) < 2:
                continue
            rows.append(cells)
            label = re.sub(r"[*_`]", "", cells[0]).strip()
            nums: List[float] = []
            unit = ""
            for cell in cells[1:]:
                num_match = re.search(r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(%|ms|s|x|MB|GB|k|M|\$)?", cell)
                if num_match:
                    val = float(num_match.group(1).replace(",", ""))
                    nums.append(val)
                    if not unit and num_match.group(2):
                        unit = num_match.group(2)
            if label and nums:
                data_points.append(
                    {
                        "label": label[:40],
                        "value": nums[0],
                        "secondary_value": nums[1] if len(nums) > 1 else 0.0,
                        "unit": unit,
                    }
                )

        if len(data_points) < 2:
            return None

        title = f"{headers[0]} vs {headers[1]}"
        widget_id = self._make_widget_id(session_id, f"table:{title}:{len(data_points)}")
        return {
            "widget_id": widget_id,
            "session_id": session_id,
            "kind": "chart",
            "title": title,
            "subtitle": f"Interactive comparison across {len(data_points)} items",
            "spec": {
                "tier": "zero_token_table",
                "chart_type": "bar",
                "headers": headers,
                "rows": rows,
                "data_points": data_points,
                "controls": [],
                "flow_nodes": [],
            },
        }

    def _extract_controls_or_flow_widget(
        self, response_text: str, session_id: str
    ) -> Optional[Dict[str, Any]]:
        """Tier 1B: Zero-token extraction of tunable parameters or multi-step workflows."""
        # Check for 3+ tunable numeric parameters in bullet lists (e.g., "- **Batch Size**: 32")
        param_matches = re.findall(
            r"^\s*[-*]\s+\*\*([A-Za-z0-9 _/-]{3,35})\*\*:\s*(-?\d+(?:\.\d+)?)\s*(%|ms|s|MB|GB|x|tokens|req/s)?",
            response_text,
            flags=re.MULTILINE,
        )
        if len(param_matches) >= 3:
            controls: List[Dict[str, Any]] = []
            data_points: List[Dict[str, Any]] = []
            for label, val_str, unit in param_matches[:6]:
                val = float(val_str)
                key = re.sub(r"[^a-z0-9_]", "_", label.strip().lower()).strip("_")
                min_v = 0.0 if val >= 0 else val * 2.0
                max_v = max(100.0, round(val * 3.0, 2)) if val > 0 else 10.0
                step_v = 1.0 if float(val).is_integer() else round((max_v - min_v) / 50.0, 2)
                controls.append(
                    {
                        "key": key,
                        "label": label.strip(),
                        "control_type": "slider",
                        "min": min_v,
                        "max": max_v,
                        "step": step_v or 0.1,
                        "default_value": val,
                        "unit": unit or "",
                    }
                )
                data_points.append(
                    {
                        "label": label.strip(),
                        "value": val,
                        "secondary_value": 0.0,
                        "unit": unit or "",
                    }
                )
            widget_id = self._make_widget_id(session_id, f"controls:{controls[0]['key']}")
            return {
                "widget_id": widget_id,
                "session_id": session_id,
                "kind": "controls",
                "title": "Interactive Parameter Explorer",
                "subtitle": "Adjust sliders to simulate parameter sensitivity or trigger a skill run",
                "spec": {
                    "tier": "zero_token_controls",
                    "chart_type": "bar",
                    "data_points": data_points,
                    "controls": controls,
                    "flow_nodes": [],
                    "output_metric_label": "Weighted Composite Score",
                    "output_metric_unit": "pts",
                },
            }

        # Check for sequential numbered workflow steps (3+ numbered steps with bold headers)
        step_matches = re.findall(
            r"^\s*(\d+)\.\s+\*\*([^*]{3,50})\*\*[:\s-]*([^\n]{5,140})",
            response_text,
            flags=re.MULTILINE,
        )
        if len(step_matches) >= 3:
            flow_nodes = [
                {
                    "id": f"step_{idx}",
                    "label": title.strip(),
                    "detail": detail.strip(),
                    "status": "ready",
                }
                for idx, title, detail in step_matches[:7]
            ]
            widget_id = self._make_widget_id(session_id, f"flow:{flow_nodes[0]['label']}")
            return {
                "widget_id": widget_id,
                "session_id": session_id,
                "kind": "flow",
                "title": "Workflow Execution Pipeline",
                "subtitle": f"{len(flow_nodes)}-stage interactive process flow",
                "spec": {
                    "tier": "zero_token_flow",
                    "chart_type": "bar",
                    "data_points": [],
                    "controls": [],
                    "flow_nodes": flow_nodes,
                },
            }

        return None

    def _synthesize_llm_widget(
        self, response_text: str, session_id: str, ctx: Any
    ) -> Optional[Dict[str, Any]]:
        """Tier 2: Single structured LLM call (`ctx.llm.complete_structured`) for prose-heavy outputs."""
        if ctx is None or getattr(ctx, "llm", None) is None:
            return None

        try:
            res = ctx.llm.complete_structured(
                instructions=(
                    "You are the Hermes Native GenUI Visual Synthesizer. Inspect the assistant's final response. "
                    "Determine if the prose explanation, quantitative comparison, architecture description, or "
                    "trade-off analysis would be clearer accompanied by an interactive Hermes GenUI widget card "
                    "('chart', 'flow', 'controls', or 'metrics'). "
                    "If the response is just a brief greeting or trivial one-liner, set `should_render_widget` to false. "
                    "Otherwise, set `should_render_widget` to true and populate the appropriate `data_points`, "
                    "`flow_nodes`, or interactive `controls` (sliders/toggles/segments) so the user can visually "
                    "explore or manipulate the concepts."
                ),
                input=[{"type": "text", "text": response_text[:4500]}],
                json_schema=GENUI_STRUCTURED_SCHEMA,
                schema_name="genui_widget_synthesis",
                purpose="Synthesize interactive GenUI widget card from final assistant prose",
                temperature=0.0,
                max_tokens=1100,
                task="reflex_genui",
            )
            parsed = getattr(res, "parsed", None)
            if not isinstance(parsed, dict) and getattr(res, "text", None):
                parsed = json.loads(res.text)
            if not isinstance(parsed, dict) or not parsed.get("should_render_widget"):
                return None

            kind = str(parsed.get("kind", "chart"))
            title = str(parsed.get("title") or "Interactive Visual Summary")
            subtitle = str(parsed.get("subtitle") or "Synthesized from assistant response")
            widget_id = self._make_widget_id(session_id, f"llm:{kind}:{title}")
            return {
                "widget_id": widget_id,
                "session_id": session_id,
                "kind": kind,
                "title": title,
                "subtitle": subtitle,
                "spec": {
                    "tier": "llm_structured",
                    "chart_type": parsed.get("chart_type", "bar"),
                    "data_points": parsed.get("data_points", []),
                    "flow_nodes": parsed.get("flow_nodes", []),
                    "controls": parsed.get("controls", []),
                    "output_metric_label": parsed.get("output_metric_label", "Simulated Output"),
                    "output_metric_unit": parsed.get("output_metric_unit", ""),
                    "action_skill_name": parsed.get("action_skill_name", ""),
                },
            }
        except Exception:
            return None

    def transform_output(
        self,
        response_text: str,
        session_id: str = "",
        ctx: Any = None,
    ) -> Optional[str]:
        """Run Two-Tier GenUI analysis on `response_text` and append `::reflex-ui{...}` if warranted."""
        if not bool(self.config.get("enable_genui_transform", True)):
            return None
        if not response_text or not isinstance(response_text, str):
            return None
        if "::reflex-ui{" in response_text:
            return None

        # Tier 1: Zero-token fast path (tables, parameter lists, or numbered workflows)
        widget = self._extract_markdown_table_widget(response_text, session_id=session_id)
        if widget is None:
            widget = self._extract_controls_or_flow_widget(response_text, session_id=session_id)

        # Tier 2: Single final LLM call (`ctx.llm.complete_structured`) for prose paragraphs
        min_chars = int(self.config.get("min_prose_chars_for_genui_llm", 280))
        if (
            widget is None
            and bool(self.config.get("enable_genui_llm_pass", True))
            and len(response_text.strip()) >= min_chars
        ):
            widget = self._synthesize_llm_widget(response_text, session_id=session_id, ctx=ctx)

        if widget is None:
            return None

        saved = self.store.upsert_widget(widget)
        directive = format_reflex_directive(
            widget_id=saved["widget_id"],
            kind=saved["kind"],
            title=saved["title"],
            pinned=bool(saved.get("pinned", False)),
        )

        # Push real-time desktop event if running inside Hermes Desktop/Web gateway
        if ctx is not None and hasattr(ctx, "emit_to_session") and session_id:
            try:
                ctx.emit_to_session(session_id, "reflex.widget.created", saved)
            except Exception:
                pass
        try:
            from hermes_cli.plugin_events import broadcast_plugin_event  # type: ignore

            broadcast_plugin_event("hermes-reflex", "widget.created", saved)
        except Exception:
            pass

        return f"{response_text.rstrip()}\n\n{directive}\n"
