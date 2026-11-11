# `hermes-reflex` — Skill-to-Code Reflex Compiler, Archive Healer & Sticky-Note GenUI for Hermes

`hermes-reflex` is a unified plugin for **Hermes Agent (`>=0.23.0`)** that bridges saved procedural `.md` skills and executable Python code while upgrading final assistant responses into interactive native Hermes UI cards that can be dragged and pinned to a permanent right-hand **Sticky-Note Sidepane**.

## Key Capabilities

### 1. Multi-Stage Skill-to-Code Reflex Engine (`compiler.py`)
- **Stage 1 — Parameter & Variable Extraction**: Uses `ctx.llm.complete_structured` (registered via `ctx.register_auxiliary_task("reflex_compiler")`) to inspect `SKILL.md` and generate `${HERMES_SKILL_DIR}/scripts/parameters_schema.json` with typed parameters, required flags, and default values.
- **Stage 2 — Executable Runner Synthesis & Prose Coexistence**: Generates `${HERMES_SKILL_DIR}/scripts/reflex_runner.py` with `--params-json` and `--dry-run` support, and writes a bounded `<!-- HERMES_REFLEX_START -->` section into `SKILL.md` so prose `.md` instructions and executable code coexist cleanly.
- **Stage 3 — 3-Gate Pre-Trust Verification**:
  1. **Gate A (AST & Safety Gate)**: Validates syntax via `ast.parse`, checks required entrypoints (`run_skill`, `main`), and blocks unsafe calls (`eval`, `exec`, destructive shell patterns).
  2. **Gate B (Synthetic Dry-Run Verification)**: Runs LLM-generated test payloads against `reflex_runner.py --dry-run` in an isolated subprocess, with bounded LLM self-repair if a test fails.
  3. **Gate C (Shadow A/B Promotion)**: Promotes skills from `TESTED` to `TRUSTED` once runtime latency confirms `avg_code_ms <= avg_prose_ms * shadow_speedup_ratio` (`0.9`).
- **Stage 4 — Live Skill Updates & Reverse Escalation**: Computes `dir_hash(skill_dir)` and listens to `on_skill_lifecycle(reuse_after_patch=True)`. Editing `SKILL.md` automatically triggers re-compilation and re-testing. If a runner encounters missing parameters or runtime errors, it emits `"status": "ESCALATE"` so Hermes falls back immediately to the prose `SKILL.md`.

### 2. Curator `.archive/` Black-Hole Healer (`archive_healer.py`)
- Addresses [Hermes Issue #148863](https://github.com/NousResearch/hermes-agent/issues/148863) where newly created procedural skills get moved into `<skills_root>/.archive/` before accumulating enough usage runs.
- Evaluates archived skills with Bayesian Laplace smoothing:
  $$\text{Score} = \frac{\text{successes} + 2.0}{\text{uses} + 3.0} + \text{procedural\_bonus}$$
- Automatically restores qualifying procedural skills back to `<skills_root>/<category>/<name>/` and compiles them into verified executable reflexes.

### 3. Two-Tier Final-Output GenUI & Sticky-Note Sidepane (`genui.py`, `desktop/index.js`)
- **Tier 1 (Zero-Token Fast Path)**: Instantly converts Markdown tables with numeric metrics, tunable parameter lists, and numbered multi-step workflows into interactive `::reflex-ui{id="..." kind="..." title="..."}` cards.
- **Tier 2 (Single Structured LLM Visual Pass)**: On substantive prose responses (`>= 280` chars), invokes one `ctx.llm.complete_structured(task="reflex_genui")` call to synthesize interactive bar/line charts, flow diagrams, or directly manipulable slider/toggle calculators.
- **Draggable Sticky-Note Sidepane (`PANES_AREA`)**: Every `::reflex-ui` card in the chat transcript is draggable (`draggable: true`) and pinnable into the right-hand **Pinned Widgets** pane, with sticky-note color tags (`amber`, `emerald`, `cyan`, `violet`), custom user annotations, live slider recalculation, and SQLite persistence across restarts.

## Installation

Copy or symlink `hermes-reflex` into `~/.hermes/plugins/hermes-reflex/`:

```bash
mkdir -p ~/.hermes/plugins
cp -r hermes-reflex ~/.hermes/plugins/hermes-reflex
```
