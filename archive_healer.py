"""Curator `.archive/` Black-Hole Healer for Hermes Skills.

Addresses Hermes Issue #148863 where the background Curator moves skills into
`<skills_root>/.archive/<category>/<skill_name>/`, making them invisible to `find_all_skills()`
and `locate_skill()` before they accumulate enough usage runs.

Capabilities:
1. Scans `<skills_root>/.archive/` and evaluates archived skills using a Bayesian Laplace-smoothed
   score: Score = (successes + 2.0) / (uses + 3.0) + procedural bonus.
2. Restores high-value procedural skills from `.archive/` back into active `<skills_root>/<category>/<name>/`
   and automatically compiles them into verified executable reflexes.
3. Hooks `on_skill_lifecycle(action="archived" | "stale")` and `post_curator_run` so newly archived
   procedural skills are immediately audited and rescued when appropriate.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .compiler import ReflexSkillCompiler, parse_skill_frontmatter
    from .store import ReflexStore
except (ImportError, ValueError):
    from compiler import ReflexSkillCompiler, parse_skill_frontmatter
    from store import ReflexStore


class ArchiveHealer:
    """Audits and heals skills trapped in the Hermes `.archive/` directory."""

    def __init__(
        self,
        store: ReflexStore,
        compiler: ReflexSkillCompiler,
        rescue_threshold: float = 0.55,
    ) -> None:
        self.store = store
        self.compiler = compiler
        self.rescue_threshold = rescue_threshold

    @property
    def archive_root(self) -> Path:
        return self.compiler.skills_root / ".archive"

    def evaluate_archived_skill(self, skill_md_path: Path) -> Dict[str, Any]:
        """Compute Bayesian Laplace score and procedural richness for an archived skill."""
        skill_dir = skill_md_path.parent
        raw = skill_md_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_skill_frontmatter(raw, skill_dir.name)
        skill_name = parsed["name"]

        existing = self.store.get_compiled_skill(skill_name)
        use_count = int(existing["use_count"]) if existing else 0
        success_count = int(existing["success_count"]) if existing else 0

        base_bayesian = self.store.compute_bayesian_score(
            success_count=success_count,
            use_count=use_count,
            prior_alpha=2.0,
            prior_beta=1.0,
        )

        body = parsed["body"]
        code_fences = len(re.findall(r"```", body)) // 2
        numbered_steps = len(re.findall(r"^\s*\d+\.\s+", body, flags=re.MULTILINE))
        has_commands = code_fences >= 1 or numbered_steps >= 2

        procedural_bonus = 0.12 if has_commands else 0.0
        cold_start_protected = use_count < 3 and has_commands
        effective_score = round(min(1.0, base_bayesian + procedural_bonus), 4)

        rel_parts = skill_md_path.relative_to(self.archive_root).parts
        category = rel_parts[0] if len(rel_parts) >= 3 else (parsed["category"] or "general")

        should_rescue = effective_score >= self.rescue_threshold and (has_commands or cold_start_protected)
        return {
            "skill_name": skill_name,
            "category": category,
            "archived_dir": str(skill_dir),
            "use_count": use_count,
            "success_count": success_count,
            "bayesian_score": effective_score,
            "has_procedural_steps": has_commands,
            "cold_start_protected": cold_start_protected,
            "should_rescue": should_rescue,
            "reason": (
                f"Bayesian score {effective_score:.2f} >= {self.rescue_threshold:.2f} "
                f"(uses={use_count}, code_blocks={code_fences}, steps={numbered_steps})"
            ),
        }

    def scan_archive(self) -> List[Dict[str, Any]]:
        """Scan `<skills_root>/.archive/` and return evaluation cards for all archived skills."""
        if not self.archive_root.exists():
            return []
        evaluations: List[Dict[str, Any]] = []
        for skill_md in sorted(self.archive_root.rglob("SKILL.md")):
            evaluations.append(self.evaluate_archived_skill(skill_md))
        return evaluations

    def rescue_skill(
        self,
        skill_name: str,
        compile_after_rescue: bool = True,
        ctx: Any = None,
    ) -> Dict[str, Any]:
        """Restore a specific archived skill back to the active Hermes skills tree and compile it."""
        candidates = [
            ev
            for ev in self.scan_archive()
            if ev["skill_name"] == skill_name or Path(ev["archived_dir"]).name == skill_name
        ]
        if not candidates:
            return {
                "ok": False,
                "skill_name": skill_name,
                "error": f"Skill '{skill_name}' was not found in {self.archive_root}",
            }

        target = candidates[0]
        src_dir = Path(target["archived_dir"])
        dest_dir = self.compiler.skills_root / target["category"] / src_dir.name
        dest_dir.parent.mkdir(parents=True, exist_ok=True)

        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(src_dir), str(dest_dir))

        event = self.store.record_archive_event(
            event_id=str(uuid.uuid4()),
            skill_name=target["skill_name"],
            action="restored",
            previous_path=str(src_dir),
            restored_path=str(dest_dir),
            bayesian_score=target["bayesian_score"],
            reason=target["reason"],
        )

        compile_result: Optional[Dict[str, Any]] = None
        if compile_after_rescue:
            compile_result = self.compiler.compile_skill(target["skill_name"], ctx=ctx, force=True)

        return {
            "ok": True,
            "skill_name": target["skill_name"],
            "restored_path": str(dest_dir),
            "bayesian_score": target["bayesian_score"],
            "event": event,
            "compiled": compile_result,
        }

    def audit_and_heal_all(
        self,
        auto_rescue: bool = True,
        compile_rescued: bool = True,
        ctx: Any = None,
    ) -> Dict[str, Any]:
        """Audit all skills in `.archive/` and optionally rescue all qualifying procedural skills."""
        scanned = self.scan_archive()
        rescued: List[Dict[str, Any]] = []
        retained_in_archive: List[Dict[str, Any]] = []

        for item in scanned:
            if auto_rescue and item["should_rescue"]:
                res = self.rescue_skill(
                    item["skill_name"],
                    compile_after_rescue=compile_rescued,
                    ctx=ctx,
                )
                if res.get("ok"):
                    rescued.append(res)
                else:
                    retained_in_archive.append(item)
            else:
                retained_in_archive.append(item)

        return {
            "ok": True,
            "archive_root": str(self.archive_root),
            "total_archived_scanned": len(scanned),
            "rescued_count": len(rescued),
            "rescued": rescued,
            "retained_in_archive": retained_in_archive,
        }
