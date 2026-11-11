"""Multi-Stage Skill-to-Code Reflex Compiler & Runtime Executor for Hermes.

Pipeline stages:
1. Parameter & Variable Extraction:
   Uses `ctx.llm.complete_structured` (task="reflex_compiler") to inspect SKILL.md and synthesize
   a strict JSON Schema (`parameters_schema.json`) of runtime parameters, flags, paths, and defaults.
2. Executable Python Code Synthesis:
   Uses `ctx.llm.complete` (task="reflex_compiler") to generate `${HERMES_SKILL_DIR}/scripts/reflex_runner.py`
   that accepts injected parameters (`--params-json`, `--dry-run`, `HERMES_SKILL_PARAMS`) and emits structured
   JSON output (`{"status": "ok" | "ESCALATE", "output": "...", "metrics": {...}}`).
   Coexists cleanly with prose `.md` skills by writing a bounded `<!-- HERMES_REFLEX_START -->` section.
3. 3-Gate Pre-Trust Verification:
   - Gate A: Python `ast.parse` syntax check + static safety inspection + entrypoint check.
   - Gate B: Synthetic dry-run execution (`--dry-run`) in an isolated subprocess using LLM-generated test cases
     (with up to `max_compile_repair_attempts` LLM self-repair passes on traceback failure).
   - Gate C: Shadow A/B latency & parameter subset check (`required_params.issubset(provided_params)` and
     `avg_code_ms <= avg_prose_ms * shadow_speedup_ratio`) before promoting `TESTED` -> `TRUSTED`.
4. Live Skill Updates & Reverse Escalation:
   Detects `dir_hash(skill_dir)` mutations or `on_skill_lifecycle(reuse_after_patch=True)` events to
   automatically re-compile and re-test modified skills, and falls back to the prose `.md` skill
   immediately if `reflex_runner.py` emits `"ESCALATE"` or non-zero exit at runtime.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from agent.secret_scope import get_secret
except ImportError:
    def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
        import os as _env_mod
        return _env_mod.environ.get(name, default)

try:
    from .store import ReflexStore
except (ImportError, ValueError):
    from store import ReflexStore

REFLEX_MARKER_START = "<!-- HERMES_REFLEX_START -->"
REFLEX_MARKER_END = "<!-- HERMES_REFLEX_END -->"

FORBIDDEN_AST_CALLS = {
    "eval",
    "exec",
    "__import__",
}

FORBIDDEN_SHELL_PATTERNS = [
    r"rm\s+-rf\s+/(?:\s|$)",
    r"mkfs\.",
    r"dd\s+if=/dev/zero",
    r":\(\)\{\s*:\|:&\s*\};:",
]

PARAMETER_EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "skill_summary": {"type": "string"},
        "is_procedural": {"type": "boolean"},
        "trigger_keywords": {
            "type": "array",
            "items": {"type": "string"},
        },
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["string", "integer", "number", "boolean"],
                    },
                    "description": {"type": "string"},
                    "required": {"type": "boolean"},
                    "default_value": {"type": "string"},
                },
                "required": ["name", "type", "description", "required", "default_value"],
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "step_id": {"type": "string"},
                    "description": {"type": "string"},
                    "command_template": {"type": "string"},
                    "expected_check": {"type": "string"},
                },
                "required": ["step_id", "description", "command_template", "expected_check"],
            },
        },
    },
    "required": [
        "skill_summary",
        "is_procedural",
        "trigger_keywords",
        "parameters",
        "steps",
    ],
}

SYNTHETIC_TEST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "params_json": {"type": "string"},
                    "expect_escalate": {"type": "boolean"},
                },
                "required": ["name", "params_json", "expect_escalate"],
            },
        }
    },
    "required": ["test_cases"],
}


def compute_skill_dir_hash(skill_dir: Path) -> str:
    """Compute a deterministic hash of SKILL.md and non-generated files in a skill directory."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    raw = skill_md.read_text(encoding="utf-8", errors="replace")
    stripped = re.sub(
        rf"{re.escape(REFLEX_MARKER_START)}.*?{re.escape(REFLEX_MARKER_END)}",
        "",
        raw,
        flags=re.DOTALL,
    ).strip()
    digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]
    return digest


def parse_skill_frontmatter(skill_md_text: str, fallback_name: str) -> Dict[str, Any]:
    """Extract YAML frontmatter fields (name, description, category) from SKILL.md."""
    name = fallback_name
    description = ""
    category = "general"
    stripped = re.sub(
        rf"{re.escape(REFLEX_MARKER_START)}.*?{re.escape(REFLEX_MARKER_END)}",
        "",
        skill_md_text,
        flags=re.DOTALL,
    ).strip()

    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", stripped, flags=re.DOTALL)
    body = stripped
    if fm_match:
        fm_text, body = fm_match.group(1), fm_match.group(2)
        for line in fm_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().lower()
                v = v.strip().strip("'\"")
                if k == "name" and v:
                    name = v
                elif k == "description" and v:
                    description = v
                elif k == "category" and v:
                    category = v
    return {
        "name": name,
        "description": description,
        "category": category,
        "body": body.strip(),
        "clean_markdown": stripped,
    }


class ReflexSkillCompiler:
    """Compiles, tests, updates, and executes Hermes skills as parameterized Python reflex runners."""

    def __init__(
        self,
        store: ReflexStore,
        skills_root: Optional[Path | str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.store = store
        self.config = config or {}
        override = str(self.config.get("skills_root_override") or "").strip()
        if skills_root:
            self.skills_root = Path(skills_root).expanduser().resolve()
        elif override:
            self.skills_root = Path(override).expanduser().resolve()
        else:
            hermes_home_str = get_secret("HERMES_HOME", "~/.hermes") or "~/.hermes"
            hermes_home = Path(hermes_home_str).expanduser()
            self.skills_root = (hermes_home / "skills").resolve()

    def discover_skills(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        """Discover all skills under ~/.hermes/skills (and optionally .archive/)."""
        discovered: List[Dict[str, Any]] = []
        if not self.skills_root.exists():
            return discovered

        for skill_md in sorted(self.skills_root.rglob("SKILL.md")):
            rel_parts = skill_md.relative_to(self.skills_root).parts
            in_archive = ".archive" in rel_parts
            if in_archive and not include_archived:
                continue
            skill_dir = skill_md.parent
            raw_text = skill_md.read_text(encoding="utf-8", errors="replace")
            parsed = parse_skill_frontmatter(raw_text, skill_dir.name)
            category = rel_parts[0] if len(rel_parts) >= 3 and not in_archive else parsed["category"]
            discovered.append(
                {
                    "skill_name": parsed["name"],
                    "category": category,
                    "description": parsed["description"],
                    "skill_dir": str(skill_dir),
                    "skill_md_path": str(skill_md),
                    "in_archive": in_archive,
                    "dir_hash": compute_skill_dir_hash(skill_dir),
                    "body": parsed["body"],
                    "clean_markdown": parsed["clean_markdown"],
                }
            )
        return discovered

    def locate_skill_dir(self, skill_name: str) -> Optional[Path]:
        """Locate a skill directory by name using filesystem scan."""
        for item in self.discover_skills(include_archived=False):
            if item["skill_name"] == skill_name or Path(item["skill_dir"]).name == skill_name:
                return Path(item["skill_dir"])
        return None

    def extract_parameters_schema(
        self, skill_info: Dict[str, Any], ctx: Any = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any], List[str], List[Dict[str, Any]]]:
        """Stage 1: Extract dynamic parameters, JSON schema, defaults, and steps from SKILL.md."""
        body = skill_info["body"]
        skill_name = skill_info["skill_name"]
        extracted: Optional[Dict[str, Any]] = None

        if ctx is not None and getattr(ctx, "llm", None) is not None:
            try:
                res = ctx.llm.complete_structured(
                    instructions=(
                        "Analyze this Hermes SKILL.md document. Extract all dynamic parameters/variables "
                        "(e.g., file paths, CLI flags, environment variables, numeric thresholds, target URLs), "
                        "trigger keywords, and sequential execution steps so this skill can be compiled into "
                        "a parameterized Python reflex script."
                    ),
                    input=[
                        {
                            "type": "text",
                            "text": f"Skill Name: {skill_name}\nDescription: {skill_info.get('description', '')}\n\n{body}",
                        }
                    ],
                    json_schema=PARAMETER_EXTRACTION_SCHEMA,
                    schema_name="skill_parameter_schema",
                    purpose=f"Extract parameter schema for skill {skill_name}",
                    temperature=0.0,
                    max_tokens=1200,
                    task="reflex_compiler",
                )
                if getattr(res, "parsed", None) and isinstance(res.parsed, dict):
                    extracted = res.parsed
                elif getattr(res, "text", None):
                    extracted = json.loads(res.text)
            except Exception:
                extracted = None

        if not extracted:
            extracted = self._heuristic_extract_parameters(skill_info)

        properties: Dict[str, Any] = {}
        required: List[str] = []
        defaults: Dict[str, Any] = {}

        for param in extracted.get("parameters", []):
            p_name = re.sub(r"[^a-zA-Z0-9_]", "_", str(param.get("name", "")).strip()).strip("_").lower()
            if not p_name:
                continue
            p_type = str(param.get("type", "string")).lower()
            if p_type not in ("string", "integer", "number", "boolean"):
                p_type = "string"
            properties[p_name] = {
                "type": p_type,
                "description": str(param.get("description", f"Parameter {p_name}")),
            }
            raw_def = param.get("default_value", "")
            if raw_def != "" and raw_def is not None:
                defaults[p_name] = self._coerce_value(raw_def, p_type)
            if bool(param.get("required", False)) and p_name not in defaults:
                required.append(p_name)

        json_schema: Dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": f"{skill_name}_parameters",
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": True,
        }
        triggers = [str(k).lower() for k in extracted.get("trigger_keywords", []) if str(k).strip()]
        name_tokens = [w for w in re.split(r"[-_\s]+", skill_name.lower()) if len(w) >= 3]
        for tok in name_tokens:
            if tok not in triggers:
                triggers.append(tok)
        steps = extracted.get("steps", [])
        return json_schema, defaults, triggers, steps

    @staticmethod
    def _coerce_value(val: Any, p_type: str) -> Any:
        if p_type == "integer":
            try:
                return int(float(str(val)))
            except Exception:
                return 0
        if p_type == "number":
            try:
                return float(str(val))
            except Exception:
                return 0.0
        if p_type == "boolean":
            if isinstance(val, bool):
                return val
            return str(val).strip().lower() in ("true", "1", "yes", "on")
        return str(val)

    def _heuristic_extract_parameters(self, skill_info: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic parameter & command extractor when ctx.llm is offline or mocked."""
        body = skill_info["body"]
        skill_name = skill_info["skill_name"]
        params: Dict[str, Dict[str, Any]] = {}

        for match in re.finditer(
            r"(?:\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}|\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}|<([a-zA-Z_][a-zA-Z0-9_-]*)>)",
            body,
        ):
            raw = match.group(1) or match.group(2) or match.group(3)
            norm = re.sub(r"[^a-zA-Z0-9_]", "_", raw).lower()
            if norm in ("hermes_skill_dir", "skill_dir", "http", "https", "br", "div", "span"):
                continue
            if norm not in params:
                p_type = "integer" if any(k in norm for k in ("port", "count", "limit", "timeout", "retries")) else "string"
                default_val = "8080" if "port" in norm else ("30" if "timeout" in norm else "./workspace")
                params[norm] = {
                    "name": norm,
                    "type": p_type,
                    "description": f"Injected parameter '{norm}' for {skill_name}",
                    "required": False,
                    "default_value": default_val,
                }

        steps: List[Dict[str, str]] = []
        code_blocks = re.findall(r"```(?:bash|sh|shell|zsh|python)?\n(.*?)```", body, flags=re.DOTALL)
        step_idx = 1
        for block in code_blocks:
            for line in block.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                steps.append(
                    {
                        "step_id": f"step_{step_idx}",
                        "description": f"Execute workflow command {step_idx}",
                        "command_template": stripped,
                        "expected_check": "exit_code == 0",
                    }
                )
                step_idx += 1

        if not steps:
            steps.append(
                {
                    "step_id": "step_1",
                    "description": f"Run {skill_name} workflow",
                    "command_template": f"echo 'Executed {skill_name}'",
                    "expected_check": "exit_code == 0",
                }
            )

        words = [w for w in re.split(r"[^a-zA-Z0-9]+", skill_name.lower()) if len(w) >= 3]
        return {
            "skill_summary": skill_info.get("description") or f"Executable reflex for {skill_name}",
            "is_procedural": True,
            "trigger_keywords": words,
            "parameters": list(params.values()),
            "steps": steps,
        }

    def synthesize_runner_code(
        self,
        skill_info: Dict[str, Any],
        parameters_schema: Dict[str, Any],
        defaults: Dict[str, Any],
        steps: List[Dict[str, Any]],
        ctx: Any = None,
        repair_error: Optional[str] = None,
    ) -> str:
        """Stage 2: Generate executable Python runner (`reflex_runner.py`) with parameter injection."""
        skill_name = skill_info["skill_name"]
        if ctx is not None and getattr(ctx, "llm", None) is not None:
            try:
                system_prompt = (
                    "You are the Hermes Reflex Skill Compiler. Write a standalone, clean Python 3 script "
                    "for `${HERMES_SKILL_DIR}/scripts/reflex_runner.py`. "
                    "Requirements:\n"
                    "1. Must define `run_skill(params: dict, dry_run: bool = False) -> dict` and `main()`.\n"
                    "2. Must parse `--params-json` and `--dry-run` via `argparse`, and fallback to `HERMES_SKILL_PARAMS` env var.\n"
                    "3. Must validate required parameters and apply defaults from `DEFAULT_PARAMS`.\n"
                    "4. If a required parameter is missing or runtime preconditions fail, return "
                    "`{'status': 'ESCALATE', 'reason': '...', 'output': ''}` so Hermes Reverse Escalation falls back to prose SKILL.md.\n"
                    "5. In `dry_run=True` mode, simulate external commands safely without mutating the filesystem and return "
                    "`{'status': 'ok', 'dry_run': True, 'resolved_commands': [...], 'params': params, 'output': '...'}`.\n"
                    "6. Print JSON output on stdout and exit with 0. Do not use `eval` or `exec`.\n"
                    "Return ONLY valid Python source code without markdown fences."
                )
                user_prompt = (
                    f"Skill Name: {skill_name}\n"
                    f"Parameter Schema: {json.dumps(parameters_schema)}\n"
                    f"Default Params: {json.dumps(defaults)}\n"
                    f"Steps: {json.dumps(steps)}\n"
                )
                if repair_error:
                    user_prompt += f"\nPrevious verification error to fix:\n{repair_error}\n"

                res = ctx.llm.complete(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=2000,
                    temperature=0.0,
                    purpose=f"Synthesize executable reflex_runner.py for {skill_name}",
                    task="reflex_compiler",
                )
                candidate = getattr(res, "text", "") or ""
                candidate = re.sub(r"^```(?:python)?\s*\n", "", candidate.strip())
                candidate = re.sub(r"\n```\s*$", "", candidate.strip())
                if "def run_skill" in candidate and "def main" in candidate:
                    return candidate
            except Exception:
                pass

        return self._render_deterministic_runner(skill_name, parameters_schema, defaults, steps)

    def _render_deterministic_runner(
        self,
        skill_name: str,
        parameters_schema: Dict[str, Any],
        defaults: Dict[str, Any],
        steps: List[Dict[str, Any]],
    ) -> str:
        """Render a rock-solid, self-contained parameterized Python runner script."""
        schema_json_literal = repr(json.dumps(parameters_schema, sort_keys=True))
        defaults_json_literal = repr(json.dumps(defaults, sort_keys=True))
        steps_json_literal = repr(json.dumps(steps, sort_keys=True))

        return f'''#!/usr/bin/env python3
"""Auto-generated Hermes Executable Reflex Runner for skill '{skill_name}'.

Coexists with SKILL.md. Supports runtime parameter injection, dry-run sandbox
verification, and Reverse Escalation ('ESCALATE') back to prose SKILL.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

SKILL_NAME = {skill_name!r}
PARAMETERS_SCHEMA: Dict[str, Any] = json.loads({schema_json_literal})
DEFAULT_PARAMS: Dict[str, Any] = json.loads({defaults_json_literal})
WORKFLOW_STEPS: List[Dict[str, Any]] = json.loads({steps_json_literal})


def _coerce_param(value: Any, expected_type: str) -> Any:
    if expected_type == "integer":
        return int(float(str(value)))
    if expected_type == "number":
        return float(str(value))
    if expected_type == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    return str(value)


def resolve_parameters(injected: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(DEFAULT_PARAMS)
    props = PARAMETERS_SCHEMA.get("properties", {{}})
    for key, val in (injected or {{}}).items():
        norm_key = re.sub(r"[^a-zA-Z0-9_]", "_", str(key).strip()).lower()
        if val is not None and val != "":
            expected_type = props.get(norm_key, {{}}).get("type", "string")
            merged[norm_key] = _coerce_param(val, expected_type)
    return merged


def substitute_template(template: str, params: Dict[str, Any], skill_dir: Path) -> str:
    rendered = template.replace("${{HERMES_SKILL_DIR}}", str(skill_dir))
    for key, val in params.items():
        str_val = str(val)
        rendered = re.sub(r"\\{{\\{{\\s*" + re.escape(key) + r"\\s*\\}}\\}}", str_val, rendered, flags=re.IGNORECASE)
        rendered = re.sub(r"\\$\\{{" + re.escape(key) + r"\\}}", str_val, rendered, flags=re.IGNORECASE)
        rendered = re.sub(r"<" + re.escape(key) + r">", str_val, rendered, flags=re.IGNORECASE)
    return rendered


def run_skill(params: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    start_ts = time.perf_counter()
    skill_dir = Path(__file__).resolve().parent.parent
    try:
        resolved_params = resolve_parameters(params)
    except Exception as exc:
        return {{
            "status": "ESCALATE",
            "skill_name": SKILL_NAME,
            "reason": f"Parameter type coercion failed: {{exc}}",
            "output": "",
        }}

    required_keys = PARAMETERS_SCHEMA.get("required", [])
    missing = [rk for rk in required_keys if rk not in resolved_params or resolved_params[rk] in ("", None)]
    if missing:
        return {{
            "status": "ESCALATE",
            "skill_name": SKILL_NAME,
            "reason": f"Missing required skill parameters: {{missing}}",
            "missing_parameters": missing,
            "output": "",
        }}

    resolved_commands: List[str] = []
    step_outputs: List[Dict[str, Any]] = []

    for step in WORKFLOW_STEPS:
        cmd_tpl = str(step.get("command_template", "")).strip()
        if not cmd_tpl:
            continue
        cmd = substitute_template(cmd_tpl, resolved_params, skill_dir)
        resolved_commands.append(cmd)
        if dry_run:
            step_outputs.append({{
                "step_id": step.get("step_id", "step"),
                "command": cmd,
                "exit_code": 0,
                "stdout": f"[DRY-RUN] Verified command: {{cmd}}",
            }})
            continue

        try:
            env = dict(os.environ)
            env["HERMES_SKILL_DIR"] = str(skill_dir)
            for pk, pv in resolved_params.items():
                env[f"SKILL_PARAM_{{pk.upper()}}"] = str(pv)
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(skill_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                return {{
                    "status": "ESCALATE",
                    "skill_name": SKILL_NAME,
                    "reason": f"Step {{step.get('step_id')}} exited with code {{proc.returncode}}: {{proc.stderr.strip()}}",
                    "failed_command": cmd,
                    "output": proc.stdout.strip(),
                }}
            step_outputs.append({{
                "step_id": step.get("step_id", "step"),
                "command": cmd,
                "exit_code": proc.returncode,
                "stdout": proc.stdout.strip(),
            }})
        except Exception as exc:
            return {{
                "status": "ESCALATE",
                "skill_name": SKILL_NAME,
                "reason": f"Subprocess execution failed: {{exc}}",
                "failed_command": cmd,
                "output": "",
            }}

    elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
    combined_stdout = "\\n".join(s["stdout"] for s in step_outputs if s.get("stdout"))
    return {{
        "status": "ok",
        "skill_name": SKILL_NAME,
        "dry_run": dry_run,
        "params": resolved_params,
        "resolved_commands": resolved_commands,
        "steps": step_outputs,
        "duration_ms": elapsed_ms,
        "output": combined_stdout or f"Completed {{len(step_outputs)}} reflex steps for {{SKILL_NAME}}.",
    }}


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Hermes Reflex Runner for {{SKILL_NAME}}")
    parser.add_argument("--params-json", type=str, default="", help="JSON dictionary of skill parameters")
    parser.add_argument("--dry-run", action="store_true", help="Validate parameters and commands without side effects")
    args = parser.parse_args()

    raw_params = args.params_json or os.environ.get("HERMES_SKILL_PARAMS", "{{}}")
    try:
        parsed_params = json.loads(raw_params) if raw_params.strip() else {{}}
        if not isinstance(parsed_params, dict):
            parsed_params = {{}}
    except Exception as exc:
        print(json.dumps({{
            "status": "ESCALATE",
            "skill_name": SKILL_NAME,
            "reason": f"Invalid JSON in --params-json: {{exc}}",
            "output": "",
        }}))
        return 0

    result = run_skill(parsed_params, dry_run=bool(args.dry_run))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

    def validate_code_ast_and_safety(self, source_code: str) -> Tuple[bool, str]:
        """Stage 3 Gate A: AST syntax validation, entrypoint check, and dangerous call filter."""
        try:
            tree = ast.parse(source_code)
        except SyntaxError as exc:
            return False, f"SyntaxError at line {exc.lineno}: {exc.msg}"

        defined_funcs = {
            node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for req_fn in ("run_skill", "main"):
            if req_fn not in defined_funcs:
                return False, f"Missing required function definition: `{req_fn}()`"

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_AST_CALLS:
                    return False, f"Forbidden dynamic call `{node.func.id}()` detected in generated runner"

        for pattern in FORBIDDEN_SHELL_PATTERNS:
            if re.search(pattern, source_code):
                return False, f"Forbidden destructive shell pattern matched: {pattern}"

        return True, "AST and safety validation passed"

    def run_synthetic_verification(
        self,
        runner_path: Path,
        parameters_schema: Dict[str, Any],
        defaults: Dict[str, Any],
        ctx: Any = None,
    ) -> Tuple[bool, str, List[Dict[str, Any]]]:
        """Stage 3 Gate B: Execute synthetic test cases in isolated dry-run subprocess."""
        test_cases: List[Dict[str, Any]] = []
        if ctx is not None and getattr(ctx, "llm", None) is not None:
            try:
                res = ctx.llm.complete_structured(
                    instructions=(
                        "Generate 2 synthetic test payloads (`params_json` as JSON string) to verify "
                        "this compiled Hermes skill runner: one valid happy-path payload and one edge-case payload."
                    ),
                    input=[
                        {
                            "type": "text",
                            "text": f"Schema: {json.dumps(parameters_schema)}\nDefaults: {json.dumps(defaults)}",
                        }
                    ],
                    json_schema=SYNTHETIC_TEST_SCHEMA,
                    schema_name="synthetic_skill_tests",
                    purpose="Generate synthetic test cases for compiled skill",
                    temperature=0.0,
                    max_tokens=600,
                    task="reflex_compiler",
                )
                parsed = getattr(res, "parsed", None)
                if isinstance(parsed, dict) and parsed.get("test_cases"):
                    test_cases = parsed["test_cases"]
            except Exception:
                test_cases = []

        if not test_cases:
            sample_params: Dict[str, Any] = dict(defaults)
            for prop_name, prop_spec in parameters_schema.get("properties", {}).items():
                if prop_name not in sample_params:
                    p_type = prop_spec.get("type", "string")
                    sample_params[prop_name] = (
                        1 if p_type == "integer" else (1.0 if p_type == "number" else (True if p_type == "boolean" else "test_val"))
                    )
            test_cases = [
                {
                    "name": "default_synthetic_happy_path",
                    "params_json": json.dumps(sample_params),
                    "expect_escalate": False,
                }
            ]

        results_log: List[Dict[str, Any]] = []
        for tc in test_cases:
            params_str = str(tc.get("params_json", "{}") or "{}")
            try:
                proc = subprocess.run(
                    [sys.executable, str(runner_path), "--dry-run", "--params-json", params_str],
                    cwd=str(runner_path.parent.parent),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except Exception as exc:
                return False, f"Synthetic test subprocess error: {exc}", results_log

            if proc.returncode != 0:
                return (
                    False,
                    f"Synthetic test '{tc.get('name')}' exited with code {proc.returncode}: {proc.stderr.strip()}",
                    results_log,
                )

            stdout = proc.stdout.strip()
            try:
                payload = json.loads(stdout)
            except Exception as exc:
                return (
                    False,
                    f"Synthetic test '{tc.get('name')}' emitted non-JSON stdout ({exc}): {stdout[:200]}",
                    results_log,
                )

            status = payload.get("status")
            if status not in ("ok", "ESCALATE"):
                return (
                    False,
                    f"Synthetic test '{tc.get('name')}' returned invalid status '{status}'",
                    results_log,
                )
            if not tc.get("expect_escalate", False) and status != "ok":
                return (
                    False,
                    f"Synthetic happy-path test '{tc.get('name')}' unexpectedly escalated: {payload.get('reason')}",
                    results_log,
                )
            results_log.append({"test_name": tc.get("name"), "result": payload})

        return True, f"Passed {len(results_log)} synthetic verification test(s)", results_log

    def _update_skill_markdown_coexistence_block(
        self,
        skill_md_path: Path,
        clean_markdown: str,
        parameters_schema: Dict[str, Any],
        defaults: Dict[str, Any],
    ) -> None:
        """Write bounded coexistence metadata into SKILL.md so prose and executable code coexist."""
        props = parameters_schema.get("properties", {})
        param_lines = []
        for k, spec in props.items():
            default_repr = defaults.get(k, "none")
            param_lines.append(f"- `{k}` (`{spec.get('type', 'string')}`, default: `{default_repr}`): {spec.get('description', '')}")
        params_bullet_text = "\n".join(param_lines) if param_lines else "- No dynamic parameters required (uses defaults)."

        coexistence_block = (
            f"\n\n{REFLEX_MARKER_START}\n"
            f"## Executable Reflex Runner (Auto-Managed by `hermes-reflex`)\n\n"
            f"This skill includes a verified executable code runner that coexists with the prose instructions above.\n\n"
            f"- **Runner Script**: `python3 ${{HERMES_SKILL_DIR}}/scripts/reflex_runner.py --params-json '<json>'`\n"
            f"- **Parameter Schema**: `${{HERMES_SKILL_DIR}}/scripts/parameters_schema.json`\n"
            f"- **Fallback Contract**: If the script outputs `\"status\": \"ESCALATE\"`, follow the prose instructions in this `SKILL.md`.\n\n"
            f"### Injected Parameters\n"
            f"{params_bullet_text}\n"
            f"{REFLEX_MARKER_END}\n"
        )
        updated = clean_markdown.rstrip() + coexistence_block
        skill_md_path.write_text(updated, encoding="utf-8")

    def compile_skill(
        self,
        skill_name: str,
        ctx: Any = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Run the full 3-stage compilation + verification pipeline for a skill."""
        skill_dir = self.locate_skill_dir(skill_name)
        if not skill_dir or not (skill_dir / "SKILL.md").exists():
            return {
                "ok": False,
                "skill_name": skill_name,
                "error": f"Skill '{skill_name}' not found under {self.skills_root}",
            }

        skill_md_path = skill_dir / "SKILL.md"
        raw_text = skill_md_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_skill_frontmatter(raw_text, skill_dir.name)
        current_hash = compute_skill_dir_hash(skill_dir)

        existing = self.store.get_compiled_skill(parsed["name"])
        if (
            existing
            and not force
            and existing.get("dir_hash") == current_hash
            and existing.get("status") in ("TESTED", "TRUSTED")
            and Path(existing.get("runner_path", "")).exists()
        ):
            return {
                "ok": True,
                "skipped_recompile": True,
                "reason": "dir_hash unchanged and runner already verified",
                "skill": existing,
            }

        skill_info = {
            "skill_name": parsed["name"],
            "category": parsed["category"],
            "description": parsed["description"],
            "skill_dir": str(skill_dir),
            "skill_md_path": str(skill_md_path),
            "body": parsed["body"],
            "clean_markdown": parsed["clean_markdown"],
        }

        # Stage 1: Extract parameter schema & defaults
        parameters_schema, defaults, triggers, steps = self.extract_parameters_schema(skill_info, ctx=ctx)

        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        runner_path = scripts_dir / "reflex_runner.py"
        schema_path = scripts_dir / "parameters_schema.json"
        schema_path.write_text(
            json.dumps({"schema": parameters_schema, "defaults": defaults, "steps": steps}, indent=2),
            encoding="utf-8",
        )

        # Stage 2 & 3: Synthesize code + run 3-gate verification (with bounded repair loop)
        max_repairs = int(self.config.get("max_compile_repair_attempts", 1))
        repair_error: Optional[str] = None
        ast_ok = False
        synth_ok = False
        verification_msg = ""
        synthetic_logs: List[Dict[str, Any]] = []

        for _attempt in range(max_repairs + 1):
            code = self.synthesize_runner_code(
                skill_info,
                parameters_schema=parameters_schema,
                defaults=defaults,
                steps=steps,
                ctx=ctx,
                repair_error=repair_error,
            )
            ast_ok, ast_msg = self.validate_code_ast_and_safety(code)
            if not ast_ok:
                repair_error = ast_msg
                verification_msg = ast_msg
                continue

            runner_path.write_text(code, encoding="utf-8")
            runner_path.chmod(0o755)

            synth_ok, synth_msg, synthetic_logs = self.run_synthetic_verification(
                runner_path=runner_path,
                parameters_schema=parameters_schema,
                defaults=defaults,
                ctx=ctx,
            )
            verification_msg = synth_msg
            if synth_ok:
                break
            repair_error = synth_msg

        if ast_ok and synth_ok:
            self._update_skill_markdown_coexistence_block(
                skill_md_path=skill_md_path,
                clean_markdown=parsed["clean_markdown"],
                parameters_schema=parameters_schema,
                defaults=defaults,
            )
            new_status = "TESTED"
        else:
            new_status = "DEGRADED"

        new_version = (int(existing["version"]) + 1) if existing else 1
        saved = self.store.upsert_compiled_skill(
            {
                "skill_name": parsed["name"],
                "category": parsed["category"],
                "skill_dir": str(skill_dir),
                "dir_hash": compute_skill_dir_hash(skill_dir),
                "status": new_status,
                "runner_path": str(runner_path),
                "parameters_schema": parameters_schema,
                "default_params": defaults,
                "trigger_keywords": triggers,
                "ast_valid": ast_ok,
                "synthetic_test_passed": synth_ok,
                "version": new_version,
                "last_error": "" if (ast_ok and synth_ok) else verification_msg,
            }
        )
        return {
            "ok": bool(ast_ok and synth_ok),
            "skipped_recompile": False,
            "verification_message": verification_msg,
            "synthetic_tests": synthetic_logs,
            "skill": saved,
        }

    def check_and_update_skill_if_changed(self, skill_name: str, ctx: Any = None) -> Optional[Dict[str, Any]]:
        """Stage 4: Check if SKILL.md changed on disk (dir_hash mismatch) and re-compile automatically."""
        skill_dir = self.locate_skill_dir(skill_name)
        if not skill_dir:
            return None
        current_hash = compute_skill_dir_hash(skill_dir)
        existing = self.store.get_compiled_skill(skill_name)
        if not existing or existing.get("dir_hash") != current_hash:
            return self.compile_skill(skill_name, ctx=ctx, force=True)
        return {"ok": True, "skipped_recompile": True, "skill": existing}

    def execute_skill(
        self,
        skill_name: str,
        params: Optional[Dict[str, Any]] = None,
        session_id: str = "",
        dry_run: bool = False,
        ctx: Any = None,
    ) -> Dict[str, Any]:
        """Execute a compiled skill with injected parameters, shadow timing check, and Reverse Escalation."""
        injected_params = params or {}
        update_check = self.check_and_update_skill_if_changed(skill_name, ctx=ctx)
        record = self.store.get_compiled_skill(skill_name)
        if not record and update_check and update_check.get("skill"):
            record = update_check["skill"]

        if not record or not record.get("runner_path") or not Path(record["runner_path"]).exists():
            return {
                "ok": False,
                "status": "ESCALATE",
                "skill_name": skill_name,
                "reason": f"No verified executable runner available for '{skill_name}'; use prose SKILL.md.",
            }

        schema = record.get("parameters_schema", {})
        required_params = set(schema.get("required", []))
        available_params = set((record.get("default_params") or {}).keys()) | set(injected_params.keys())
        if not required_params.issubset(available_params):
            missing = sorted(required_params - available_params)
            return {
                "ok": False,
                "status": "ESCALATE",
                "skill_name": skill_name,
                "reason": f"Required parameters {missing} not satisfied; escalating to prose SKILL.md.",
                "missing_parameters": missing,
            }

        runner_path = Path(record["runner_path"])
        cmd = [sys.executable, str(runner_path), "--params-json", json.dumps(injected_params)]
        if dry_run:
            cmd.append("--dry-run")

        start_ts = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(runner_path.parent.parent),
                capture_output=True,
                text=True,
                timeout=35,
            )
            duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
            self.store.record_execution(
                run_id=str(uuid.uuid4()),
                skill_name=skill_name,
                session_id=session_id,
                mode="escalated",
                params=injected_params,
                duration_ms=duration_ms,
                success=False,
                escalated=True,
                output_preview=str(exc),
                shadow_speedup_ratio=float(self.config.get("shadow_speedup_ratio", 0.9)),
            )
            return {
                "ok": False,
                "status": "ESCALATE",
                "skill_name": skill_name,
                "reason": f"Runner subprocess failed ({exc}); falling back to prose SKILL.md.",
            }

        try:
            payload = json.loads(proc.stdout.strip())
        except Exception:
            payload = {
                "status": "ESCALATE",
                "reason": f"Non-JSON output from runner (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}",
                "output": proc.stdout.strip(),
            }

        escalated = payload.get("status") == "ESCALATE" or proc.returncode != 0
        success = not escalated and payload.get("status") == "ok"

        updated_skill = self.store.record_execution(
            run_id=str(uuid.uuid4()),
            skill_name=skill_name,
            session_id=session_id,
            mode="escalated" if escalated else ("shadow" if record.get("status") == "TESTED" else "code"),
            params=injected_params,
            duration_ms=duration_ms,
            success=success,
            escalated=escalated,
            output_preview=str(payload.get("output", "")),
            shadow_speedup_ratio=float(self.config.get("shadow_speedup_ratio", 0.9)),
        )

        return {
            "ok": success,
            "status": payload.get("status", "ESCALATE"),
            "skill_name": skill_name,
            "skill_status": updated_skill.get("status", record.get("status")),
            "bayesian_score": updated_skill.get("bayesian_score", record.get("bayesian_score")),
            "duration_ms": duration_ms,
            "result": payload,
        }

    def match_skills_for_user_message(self, user_message: str, ctx: Any = None) -> List[Dict[str, Any]]:
        """Match compiled (or lazily compile matching active) skills against an incoming user prompt."""
        if not user_message or len(user_message.strip()) < 3:
            return []
        msg_lower = user_message.lower()

        # Lazy on-demand compilation for any uncompiled active skill matching the user message
        if bool(self.config.get("auto_compile_on_patch", True)):
            compiled_map = {s["skill_name"]: s for s in self.store.list_compiled_skills()}
            for item in self.discover_skills(include_archived=False):
                s_name = item["skill_name"]
                if s_name in compiled_map:
                    continue
                tokens = [w for w in re.split(r"[-_\s]+", s_name.lower()) if len(w) >= 3]
                if s_name.lower() in msg_lower or any(t in msg_lower for t in tokens):
                    self.compile_skill(s_name, ctx=ctx)

        matches: List[Dict[str, Any]] = []
        for skill in self.store.list_compiled_skills():
            if skill.get("status") not in ("TESTED", "TRUSTED"):
                continue
            name_lower = skill["skill_name"].lower()
            keywords = [str(k).lower() for k in skill.get("trigger_keywords", [])]
            if name_lower in msg_lower or any(k and len(k) >= 3 and k in msg_lower for k in keywords):
                matches.append(skill)
        return matches[:3]
