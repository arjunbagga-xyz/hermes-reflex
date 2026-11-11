"""End-to-End Automated Test Suite for `hermes-reflex`.

Verifies:
1. Hermes Manifest v2 (`plugin.yaml`) schema compliance & zero `velvet`/`shen` naming.
2. Strict Hermes tool & hook type contracts (all tools return `str` via `json.dumps`;
   `transform_llm_output` returns `str | None`; `pre_llm_call` returns `dict | None`).
3. Multi-stage LLM skill compilation (`ctx.llm.complete_structured` + `ctx.llm.complete`),
   parameter extraction & injection, 3-gate verification (AST + synthetic dry-run + shadow promotion),
   auto-repair on syntax/runtime error, prose `.md` coexistence, live `SKILL.md` updates,
   and Reverse Escalation (`ESCALATE`).
4. Curator `.archive/` black-hole audit & Bayesian Laplace rescue (Issue #148863).
5. Two-tier GenUI (`transform_llm_output`) zero-token + structured LLM visual pass,
   strict `::reflex-ui{key="value"}` directive formatting, and Sticky-Note Sidepane persistence.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

# Add workspace root and plugin dir to sys.path so `hermes-reflex` can be imported
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import importlib

hermes_reflex = importlib.import_module("hermes-reflex")
dashboard_api = importlib.import_module("hermes-reflex.dashboard.plugin_api")


class FakeLlmResult:
    def __init__(self, text: str = "", parsed: Any = None, content_type: str = "json") -> None:
        self.text = text
        self.parsed = parsed
        self.content_type = content_type


class MockPluginLlm:
    """Simulates Hermes `ctx.llm` (`complete` and `complete_structured`) with call tracking."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.inject_bad_syntax_once: bool = False

    def complete_structured(
        self,
        instructions: str,
        input: List[Dict[str, Any]],
        json_schema: Dict[str, Any],
        schema_name: str = "output",
        purpose: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1000,
        task: str = "",
        **kwargs: Any,
    ) -> FakeLlmResult:
        self.calls.append(
            {
                "method": "complete_structured",
                "schema_name": schema_name,
                "purpose": purpose,
                "task": task,
            }
        )
        text_input = input[0].get("text", "") if input else ""

        if schema_name == "skill_parameter_schema":
            has_region = "region" in text_input.lower()
            params = [
                {
                    "name": "target_env",
                    "type": "string",
                    "description": "Deployment environment name",
                    "required": True,
                    "default_value": "staging",
                },
                {
                    "name": "port",
                    "type": "integer",
                    "description": "Service port number",
                    "required": False,
                    "default_value": "8080",
                },
            ]
            cmd_tpl = "echo 'Deploying to {{target_env}} on port {{port}}'"
            if has_region:
                params.append(
                    {
                        "name": "region",
                        "type": "string",
                        "description": "Cloud region",
                        "required": False,
                        "default_value": "ap-south-1",
                    }
                )
                cmd_tpl = "echo 'Deploying to {{target_env}} on port {{port}} in {{region}}'"

            parsed = {
                "skill_summary": "Deploy service to target environment",
                "is_procedural": True,
                "trigger_keywords": ["deploy", "rollout", "staging"],
                "parameters": params,
                "steps": [
                    {
                        "step_id": "step_1",
                        "description": "Run deployment command",
                        "command_template": cmd_tpl,
                        "expected_check": "exit_code == 0",
                    }
                ],
            }
            return FakeLlmResult(text=json.dumps(parsed), parsed=parsed)

        if schema_name == "synthetic_skill_tests":
            parsed = {
                "test_cases": [
                    {
                        "name": "llm_synthetic_staging_test",
                        "params_json": json.dumps({"target_env": "staging", "port": 9000}),
                        "expect_escalate": False,
                    }
                ]
            }
            return FakeLlmResult(text=json.dumps(parsed), parsed=parsed)

        if schema_name == "genui_widget_synthesis":
            parsed = {
                "should_render_widget": True,
                "kind": "controls",
                "title": "Throughput vs Batch Size Simulator",
                "subtitle": "Synthesized via Tier-2 structured LLM GenUI pass",
                "chart_type": "bar",
                "data_points": [
                    {"label": "Batch 16", "value": 420.0, "secondary_value": 12.0, "unit": "tok/s"},
                    {"label": "Batch 32", "value": 780.0, "secondary_value": 19.0, "unit": "tok/s"},
                    {"label": "Batch 64", "value": 1150.0, "secondary_value": 34.0, "unit": "tok/s"},
                ],
                "flow_nodes": [],
                "controls": [
                    {
                        "key": "batch_size",
                        "label": "Batch Size",
                        "control_type": "slider",
                        "min": 8.0,
                        "max": 128.0,
                        "step": 8.0,
                        "default_value": 32.0,
                        "unit": "seqs",
                    },
                    {
                        "key": "concurrency",
                        "label": "Worker Threads",
                        "control_type": "slider",
                        "min": 1.0,
                        "max": 16.0,
                        "step": 1.0,
                        "default_value": 4.0,
                        "unit": "threads",
                    },
                ],
                "output_metric_label": "Estimated Throughput",
                "output_metric_unit": "tok/s",
                "action_skill_name": "deploy-service",
            }
            return FakeLlmResult(text=json.dumps(parsed), parsed=parsed)

        return FakeLlmResult(text="{}", parsed={})

    def complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 1500,
        temperature: float = 0.0,
        purpose: str = "",
        task: str = "",
        **kwargs: Any,
    ) -> FakeLlmResult:
        self.calls.append({"method": "complete", "purpose": purpose, "task": task})
        if self.inject_bad_syntax_once:
            self.inject_bad_syntax_once = False
            # Intentionally return forbidden eval() so Gate A rejects it and triggers the repair loop
            return FakeLlmResult(
                text="def run_skill(params, dry_run=False):\n    return eval('1+1')\ndef main():\n    pass\n"
            )
        # Return empty string so compiler uses its rock-solid template with extracted LLM schema
        return FakeLlmResult(text="")


class MockPluginContext:
    """Simulates Hermes `PluginContext` (`ctx`)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self.llm = MockPluginLlm()
        self.auxiliary_tasks: Dict[str, Dict[str, Any]] = {}
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.hooks: Dict[str, Callable[..., Any]] = {}
        self.cli_commands: Dict[str, Callable[..., Any]] = {}
        self.rpcs: Dict[str, Callable[..., Any]] = {}
        self.session_events: List[Dict[str, Any]] = []

    def get_config(self) -> Dict[str, Any]:
        return self.config

    def register_auxiliary_task(self, name: str, **kwargs: Any) -> None:
        self.auxiliary_tasks[name] = kwargs

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: Dict[str, Any],
        handler: Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_hook(self, hook_name: str, handler: Callable[..., Any]) -> None:
        self.hooks[hook_name] = handler

    def register_cli_command(self, name: str, description: str, handler: Callable[..., Any]) -> None:
        self.cli_commands[name] = handler

    def register_rpc(self, name: str, handler: Callable[..., Any]) -> None:
        self.rpcs[name] = handler

    def emit_to_session(self, session_id: str, event: str, payload: Any) -> None:
        self.session_events.append({"session_id": session_id, "event": event, "payload": payload})


class HermesReflexTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="hermes_reflex_test_")
        self.root = Path(self.temp_dir.name)
        self.skills_root = self.root / "skills"
        self.db_path = self.root / "state.db"
        self.skills_root.mkdir(parents=True, exist_ok=True)

        # Create an active procedural skill
        self.skill_dir = self.skills_root / "devops" / "deploy-service"
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        (self.skill_dir / "SKILL.md").write_text(
            """---
name: deploy-service
description: Deploy microservice to target environment and port
category: devops
---
# Deploy Service Skill

Follow these steps to deploy the service:

1. **Validate target environment**: Confirm `{{target_env}}` is reachable.
2. **Execute rollout**:
```bash
echo "Deploying to {{target_env}} on port {{port}}"
```
""",
            encoding="utf-8",
        )

        # Create an archived skill in `.archive/` simulating Issue #148863
        self.archived_dir = self.skills_root / ".archive" / "devops" / "canary-check"
        self.archived_dir.mkdir(parents=True, exist_ok=True)
        (self.archived_dir / "SKILL.md").write_text(
            """---
name: canary-check
description: Verify canary health metrics and latency thresholds
category: devops
---
# Canary Check Workflow

1. **Probe endpoint**: Run health check against `{{canary_host}}`.
2. **Verify status**:
```bash
echo "Canary healthy on {{canary_host}}"
```
""",
            encoding="utf-8",
        )

        self.ctx = MockPluginContext(
            config={
                "skills_root_override": str(self.skills_root),
                "auto_compile_on_patch": True,
                "auto_rescue_archived_skills": True,
                "enable_genui_transform": True,
                "enable_genui_llm_pass": True,
                "min_prose_chars_for_genui_llm": 120,
                "max_compile_repair_attempts": 1,
                "shadow_speedup_ratio": 0.9,
            }
        )
        import os

        os.environ["HERMES_REFLEX_DB_PATH"] = str(self.db_path)
        hermes_reflex.register(self.ctx)
        self.plugin = hermes_reflex.get_active_plugin()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_01_manifest_v2_and_zero_velvet_shen_naming(self) -> None:
        """Verify plugin.yaml obeys Hermes Manifest v2 rules and contains zero velvet/shen references."""
        manifest_path = PLUGIN_DIR / "plugin.yaml"
        self.assertTrue(manifest_path.exists())
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "hermes-reflex")
        self.assertEqual(manifest["requires_hermes"], ">=0.23.0")
        self.assertEqual(
            set(manifest["provides_tools"]),
            {"reflex_compile_skill", "reflex_run_skill", "reflex_audit_archive", "reflex_pin_widget"},
        )
        self.assertEqual(
            set(manifest["provides_hooks"]),
            {"pre_llm_call", "transform_llm_output", "on_skill_lifecycle", "post_curator_run"},
        )

        # Ensure config_schema only uses valid Hermes types: bool, int, float, str
        allowed_types = {"bool", "int", "float", "str"}
        for key, spec in manifest.get("config_schema", {}).items():
            self.assertIn(spec["type"], allowed_types, f"Invalid config_schema type for {key}: {spec['type']}")

        # Verify zero velvet/shen naming across all source files
        for src_file in [
            PLUGIN_DIR / "plugin.yaml",
            PLUGIN_DIR / "__init__.py",
            PLUGIN_DIR / "store.py",
            PLUGIN_DIR / "compiler.py",
            PLUGIN_DIR / "archive_healer.py",
            PLUGIN_DIR / "genui.py",
            PLUGIN_DIR / "dashboard" / "plugin_api.py",
            PLUGIN_DIR / "desktop" / "index.js",
        ]:
            content = src_file.read_text(encoding="utf-8").lower()
            self.assertNotIn("velvet", content, f"Found forbidden 'velvet' in {src_file.name}")
            self.assertNotIn("shen", content, f"Found forbidden 'shen' in {src_file.name}")

    def test_02_auxiliary_tasks_and_tool_string_return_contracts(self) -> None:
        """Verify auxiliary tasks are registered and EVERY tool handler returns a JSON string (never dict)."""
        self.assertIn("reflex_compiler", self.ctx.auxiliary_tasks)
        self.assertIn("reflex_genui", self.ctx.auxiliary_tasks)

        for tool_name in ("reflex_compile_skill", "reflex_run_skill", "reflex_audit_archive", "reflex_pin_widget"):
            self.assertIn(tool_name, self.ctx.tools)

        # Call each tool and assert `isinstance(res, str)`
        out_compile = self.ctx.tools["reflex_compile_skill"]["handler"](skill_name="deploy-service")
        self.assertIsInstance(out_compile, str)
        parsed_compile = json.loads(out_compile)
        self.assertTrue(parsed_compile["ok"])

        out_run = self.ctx.tools["reflex_run_skill"]["handler"](
            skill_name="deploy-service",
            params_json=json.dumps({"target_env": "production", "port": 9091}),
        )
        self.assertIsInstance(out_run, str)
        parsed_run = json.loads(out_run)
        self.assertTrue(parsed_run["ok"])
        self.assertIn("Deploying to production on port 9091", parsed_run["result"]["output"])

        out_archive = self.ctx.tools["reflex_audit_archive"]["handler"](auto_rescue=True)
        self.assertIsInstance(out_archive, str)
        parsed_archive = json.loads(out_archive)
        self.assertTrue(parsed_archive["ok"])

        out_pin = self.ctx.tools["reflex_pin_widget"]["handler"](
            action="create",
            kind="chart",
            title="Latency Breakdown",
            spec_json=json.dumps({"data_points": [{"label": "p50", "value": 12, "secondary_value": 0, "unit": "ms"}]}),
        )
        self.assertIsInstance(out_pin, str)
        parsed_pin = json.loads(out_pin)
        self.assertTrue(parsed_pin["ok"])

    def test_03_skill_compilation_self_repair_and_shadow_promotion(self) -> None:
        """Verify 3-gate compilation, AST safety repair loop, prose coexistence, and TESTED -> TRUSTED promotion."""
        # Inject one bad AST response (`eval('1+1')`) to prove Gate A catches it and self-repairs on attempt 2
        self.ctx.llm.inject_bad_syntax_once = True
        compile_res = self.plugin.compiler.compile_skill("deploy-service", ctx=self.ctx, force=True)
        self.assertTrue(compile_res["ok"])
        skill_rec = compile_res["skill"]
        self.assertEqual(skill_rec["status"], "TESTED")
        self.assertTrue(skill_rec["ast_valid"])
        self.assertTrue(skill_rec["synthetic_test_passed"])

        # Verify coexistence block in SKILL.md preserves original prose
        md_text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("# Deploy Service Skill", md_text)
        self.assertIn("<!-- HERMES_REFLEX_START -->", md_text)
        self.assertIn("reflex_runner.py", md_text)

        # Execute the compiled skill and verify promotion from TESTED -> TRUSTED (since code_ms << 1200ms * 0.9)
        exec_res = self.plugin.compiler.execute_skill(
            "deploy-service",
            params={"target_env": "canary", "port": 7070},
            session_id="sess_1",
            ctx=self.ctx,
        )
        self.assertTrue(exec_res["ok"])
        self.assertEqual(exec_res["skill_status"], "TRUSTED")
        self.assertGreater(exec_res["bayesian_score"], 0.70)

    def test_04_live_skill_update_and_reverse_escalation(self) -> None:
        """Verify editing SKILL.md triggers re-compilation with new parameters, and missing required params ESCALATE."""
        self.plugin.compiler.compile_skill("deploy-service", ctx=self.ctx, force=True)
        v1 = self.plugin.store.get_compiled_skill("deploy-service")
        self.assertIsNotNone(v1)

        # Edit SKILL.md to add a `{{region}}` parameter
        (self.skill_dir / "SKILL.md").write_text(
            """---
name: deploy-service
description: Deploy microservice to target environment, port, and cloud region
category: devops
---
# Deploy Service Skill

1. **Deploy with region**:
```bash
echo "Deploying to {{target_env}} on port {{port}} in {{region}}"
```
""",
            encoding="utf-8",
        )

        # Trigger live execution: compiler detects dir_hash change, re-extracts `region`, and re-compiles
        exec_updated = self.plugin.compiler.execute_skill(
            "deploy-service",
            params={"target_env": "prod", "port": 8443, "region": "eu-west-1"},
            ctx=self.ctx,
        )
        self.assertTrue(exec_updated["ok"])
        self.assertIn("Deploying to prod on port 8443 in eu-west-1", exec_updated["result"]["output"])
        v2 = self.plugin.store.get_compiled_skill("deploy-service")
        self.assertGreater(v2["version"], v1["version"])
        self.assertIn("region", v2["parameters_schema"]["properties"])

        # Test Reverse Escalation when a command fails or required param is explicitly blanked
        runner_path = Path(v2["runner_path"])
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(runner_path), "--params-json", "{invalid_json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(payload["status"], "ESCALATE")

    def test_05_archive_blackhole_healer_and_hooks(self) -> None:
        """Verify ArchiveHealer rescues `.archive/` skills and compiles them into active reflexes."""
        self.assertTrue((self.archived_dir / "SKILL.md").exists())
        self.proxy_hook = self.ctx.hooks["post_curator_run"]
        hook_ret = self.proxy_hook(run_dir="/tmp/curator", dry_run=False)
        self.assertIsNone(hook_ret)

        restored_md = self.skills_root / "devops" / "canary-check" / "SKILL.md"
        self.assertTrue(restored_md.exists(), "Archived procedural skill should be restored to active category")
        compiled_canary = self.plugin.store.get_compiled_skill("canary-check")
        self.assertIsNotNone(compiled_canary)
        self.assertIn(compiled_canary["status"], ("TESTED", "TRUSTED"))

        # Test `pre_llm_call` context injection
        pre_ret = self.ctx.hooks["pre_llm_call"](
            session_id="sess_1",
            user_message="Please deploy the service to staging now",
        )
        self.assertIsInstance(pre_ret, dict)
        self.assertIn("deploy-service", pre_ret["context"])

    def test_06_two_tier_genui_and_sticky_pinboard_api(self) -> None:
        """Verify Tier-1 zero-token table/flow/control extraction, Tier-2 LLM GenUI pass, and pinboard API."""
        # Tier 1: Markdown table -> ::reflex-ui{id="..." kind="chart" ...}
        table_response = (
            "Here is the benchmark comparison across runtimes:\n\n"
            "| Runtime | Throughput | Latency |\n"
            "| :--- | :--- | :--- |\n"
            "| Prose Skill | 120 req/s | 950 ms |\n"
            "| Reflex Runner | 890 req/s | 45 ms |\n"
        )
        transformed_t1 = self.ctx.hooks["transform_llm_output"](
            response_text=table_response,
            session_id="sess_ui",
            model="hermes-3",
            platform="desktop",
        )
        self.assertIsInstance(transformed_t1, str)
        match_t1 = re.search(
            r'::reflex-ui\{id="([^"]+)" kind="([^"]+)" title="([^"]+)" pinned="([^"]+)"\}',
            transformed_t1,
        )
        self.assertIsNotNone(match_t1)
        widget_id_t1 = match_t1.group(1)
        self.assertEqual(match_t1.group(2), "chart")

        # Tier 2: Prose paragraphs -> single `ctx.llm.complete_structured` call -> ::reflex-ui{...}
        prose_response = (
            "When tuning the inference service, increasing the batch size from 16 to 64 sequences "
            "substantially improves total token throughput from 420 tok/s to 1,150 tok/s, though tail "
            "latency increases moderately as worker thread contention grows across the cluster."
        )
        transformed_t2 = self.ctx.hooks["transform_llm_output"](
            response_text=prose_response,
            session_id="sess_ui",
            model="hermes-3",
            platform="desktop",
        )
        self.assertIsInstance(transformed_t2, str)
        match_t2 = re.search(
            r'::reflex-ui\{id="([^"]+)" kind="([^"]+)" title="([^"]+)" pinned="([^"]+)"\}',
            transformed_t2,
        )
        self.assertIsNotNone(match_t2)
        widget_id_t2 = match_t2.group(1)
        self.assertEqual(match_t2.group(2), "controls")

        # Pin widget to Sticky-Note Sidepane via FastAPI endpoint handler and update slider values
        pin_res = dashboard_api.update_widget_pin(
            widget_id_t2,
            {"pinned": True, "note_color": "emerald", "user_notes": "Keep batch_size=64 for nightly jobs"},
        )
        self.assertTrue(pin_res["ok"])
        self.assertTrue(pin_res["widget"]["pinned"])
        self.assertEqual(pin_res["widget"]["note_color"], "emerald")

        ctrl_res = dashboard_api.update_widget_controls(
            widget_id_t2,
            {"values": {"batch_size": 64, "concurrency": 8}},
        )
        self.assertTrue(ctrl_res["ok"])
        self.assertEqual(ctrl_res["widget"]["spec"]["last_interactive_values"]["batch_size"], 64)

        dashboard_api.compile_skill_endpoint({"compile_all": True})
        status_res = dashboard_api.get_reflex_status()
        self.assertGreaterEqual(status_res["pinned_widgets_count"], 1)
        self.assertGreaterEqual(status_res["compiled_skills_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
