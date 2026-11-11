"""SQLite persistence layer for hermes-reflex.

Stores:
1. Compiled executable skill state, parameter JSON schemas, dir_hash, and 3-gate test telemetry.
2. Execution history & shadow A/B verification benchmarks.
3. Curator .archive/ rescue ledger and Bayesian Laplace reliability scores.
4. GenUI widgets and permanently pinned sticky-note sidepane state across sessions.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from agent.secret_scope import get_secret
except ImportError:
    def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
        import os as _env_mod
        return _env_mod.environ.get(name, default)


def default_db_path() -> Path:
    env_path = (get_secret("HERMES_REFLEX_DB_PATH") or "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    hermes_home_str = get_secret("HERMES_HOME", "~/.hermes") or "~/.hermes"
    hermes_home = Path(hermes_home_str).expanduser()
    return hermes_home / "plugins" / "hermes-reflex" / "state.db"


class ReflexStore:
    """Thread-safe SQLite state store for compiled skills, archive healing, and GenUI pinboard widgets."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path).expanduser().resolve() if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS compiled_skills (
                    skill_name TEXT PRIMARY KEY,
                    category TEXT NOT NULL DEFAULT 'general',
                    skill_dir TEXT NOT NULL,
                    dir_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'CANDIDATE',
                    runner_path TEXT NOT NULL DEFAULT '',
                    parameters_schema_json TEXT NOT NULL DEFAULT '{}',
                    default_params_json TEXT NOT NULL DEFAULT '{}',
                    trigger_keywords_json TEXT NOT NULL DEFAULT '[]',
                    ast_valid INTEGER NOT NULL DEFAULT 0,
                    synthetic_test_passed INTEGER NOT NULL DEFAULT 0,
                    shadow_runs INTEGER NOT NULL DEFAULT 0,
                    shadow_successes INTEGER NOT NULL DEFAULT 0,
                    avg_prose_ms REAL NOT NULL DEFAULT 1200.0,
                    avg_code_ms REAL NOT NULL DEFAULT 0.0,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    bayesian_score REAL NOT NULL DEFAULT 0.6667,
                    version INTEGER NOT NULL DEFAULT 1,
                    last_compiled_at REAL NOT NULL DEFAULT 0.0,
                    last_error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS execution_runs (
                    run_id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'code',
                    params_json TEXT NOT NULL DEFAULT '{}',
                    duration_ms REAL NOT NULL DEFAULT 0.0,
                    success INTEGER NOT NULL DEFAULT 1,
                    escalated INTEGER NOT NULL DEFAULT 0,
                    output_preview TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS archive_events (
                    event_id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    previous_path TEXT NOT NULL DEFAULT '',
                    restored_path TEXT NOT NULL DEFAULT '',
                    bayesian_score REAL NOT NULL DEFAULT 0.0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS widgets (
                    widget_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'chart',
                    title TEXT NOT NULL DEFAULT '',
                    subtitle TEXT NOT NULL DEFAULT '',
                    spec_json TEXT NOT NULL DEFAULT '{}',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    pin_order INTEGER NOT NULL DEFAULT 0,
                    note_color TEXT NOT NULL DEFAULT 'amber',
                    user_notes TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0.0,
                    updated_at REAL NOT NULL DEFAULT 0.0
                );
                """
            )

    @staticmethod
    def compute_bayesian_score(
        success_count: int,
        use_count: int,
        prior_alpha: float = 2.0,
        prior_beta: float = 1.0,
    ) -> float:
        """Laplace-smoothed reliability score preventing cold-start archive black-holing."""
        total = max(use_count, success_count)
        return round((float(success_count) + prior_alpha) / (float(total) + prior_alpha + prior_beta), 4)

    def upsert_compiled_skill(self, record: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        skill_name = str(record["skill_name"])
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM compiled_skills WHERE skill_name = ?", (skill_name,)
            ).fetchone()

            if existing:
                use_count = int(record.get("use_count", existing["use_count"]))
                success_count = int(record.get("success_count", existing["success_count"]))
                failure_count = int(record.get("failure_count", existing["failure_count"]))
                version = int(record.get("version", existing["version"]))
            else:
                use_count = int(record.get("use_count", 0))
                success_count = int(record.get("success_count", 0))
                failure_count = int(record.get("failure_count", 0))
                version = int(record.get("version", 1))

            b_score = self.compute_bayesian_score(success_count, use_count)

            params_schema = record.get("parameters_schema")
            if isinstance(params_schema, (dict, list)):
                params_schema_str = json.dumps(params_schema)
            else:
                params_schema_str = str(
                    record.get(
                        "parameters_schema_json",
                        existing["parameters_schema_json"] if existing else "{}",
                    )
                )

            default_params = record.get("default_params")
            if isinstance(default_params, (dict, list)):
                default_params_str = json.dumps(default_params)
            else:
                default_params_str = str(
                    record.get(
                        "default_params_json",
                        existing["default_params_json"] if existing else "{}",
                    )
                )

            trigger_keywords = record.get("trigger_keywords")
            if isinstance(trigger_keywords, list):
                trigger_keywords_str = json.dumps(trigger_keywords)
            else:
                trigger_keywords_str = str(
                    record.get(
                        "trigger_keywords_json",
                        existing["trigger_keywords_json"] if existing else "[]",
                    )
                )

            conn.execute(
                """
                INSERT INTO compiled_skills (
                    skill_name, category, skill_dir, dir_hash, status, runner_path,
                    parameters_schema_json, default_params_json, trigger_keywords_json,
                    ast_valid, synthetic_test_passed, shadow_runs, shadow_successes,
                    avg_prose_ms, avg_code_ms, use_count, success_count, failure_count,
                    bayesian_score, version, last_compiled_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_name) DO UPDATE SET
                    category = excluded.category,
                    skill_dir = excluded.skill_dir,
                    dir_hash = excluded.dir_hash,
                    status = excluded.status,
                    runner_path = excluded.runner_path,
                    parameters_schema_json = excluded.parameters_schema_json,
                    default_params_json = excluded.default_params_json,
                    trigger_keywords_json = excluded.trigger_keywords_json,
                    ast_valid = excluded.ast_valid,
                    synthetic_test_passed = excluded.synthetic_test_passed,
                    shadow_runs = excluded.shadow_runs,
                    shadow_successes = excluded.shadow_successes,
                    avg_prose_ms = excluded.avg_prose_ms,
                    avg_code_ms = excluded.avg_code_ms,
                    use_count = excluded.use_count,
                    success_count = excluded.success_count,
                    failure_count = excluded.failure_count,
                    bayesian_score = excluded.bayesian_score,
                    version = excluded.version,
                    last_compiled_at = excluded.last_compiled_at,
                    last_error = excluded.last_error
                """,
                (
                    skill_name,
                    str(record.get("category", existing["category"] if existing else "general")),
                    str(record.get("skill_dir", existing["skill_dir"] if existing else "")),
                    str(record.get("dir_hash", existing["dir_hash"] if existing else "")),
                    str(record.get("status", existing["status"] if existing else "CANDIDATE")),
                    str(record.get("runner_path", existing["runner_path"] if existing else "")),
                    params_schema_str,
                    default_params_str,
                    trigger_keywords_str,
                    int(bool(record.get("ast_valid", existing["ast_valid"] if existing else 0))),
                    int(
                        bool(
                            record.get(
                                "synthetic_test_passed",
                                existing["synthetic_test_passed"] if existing else 0,
                            )
                        )
                    ),
                    int(record.get("shadow_runs", existing["shadow_runs"] if existing else 0)),
                    int(record.get("shadow_successes", existing["shadow_successes"] if existing else 0)),
                    float(record.get("avg_prose_ms", existing["avg_prose_ms"] if existing else 1200.0)),
                    float(record.get("avg_code_ms", existing["avg_code_ms"] if existing else 0.0)),
                    use_count,
                    success_count,
                    failure_count,
                    b_score,
                    version,
                    float(record.get("last_compiled_at", now)),
                    str(record.get("last_error", "")),
                ),
            )
        return self.get_compiled_skill(skill_name) or {}

    def _deserialize_skill_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["ast_valid"] = bool(item["ast_valid"])
        item["synthetic_test_passed"] = bool(item["synthetic_test_passed"])
        try:
            item["parameters_schema"] = json.loads(item.get("parameters_schema_json") or "{}")
        except Exception:
            item["parameters_schema"] = {}
        try:
            item["default_params"] = json.loads(item.get("default_params_json") or "{}")
        except Exception:
            item["default_params"] = {}
        try:
            item["trigger_keywords"] = json.loads(item.get("trigger_keywords_json") or "[]")
        except Exception:
            item["trigger_keywords"] = []
        return item

    def get_compiled_skill(self, skill_name: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM compiled_skills WHERE skill_name = ?", (skill_name,)
            ).fetchone()
            return self._deserialize_skill_row(row) if row else None

    def list_compiled_skills(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM compiled_skills WHERE status = ? ORDER BY bayesian_score DESC, skill_name ASC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM compiled_skills ORDER BY bayesian_score DESC, skill_name ASC"
                ).fetchall()
            return [self._deserialize_skill_row(r) for r in rows]

    def record_execution(
        self,
        run_id: str,
        skill_name: str,
        session_id: str,
        mode: str,
        params: Dict[str, Any],
        duration_ms: float,
        success: bool,
        escalated: bool = False,
        output_preview: str = "",
        shadow_speedup_ratio: float = 0.9,
    ) -> Dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO execution_runs (
                    run_id, skill_name, session_id, mode, params_json,
                    duration_ms, success, escalated, output_preview, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    skill_name,
                    session_id,
                    mode,
                    json.dumps(params or {}),
                    float(duration_ms),
                    1 if success else 0,
                    1 if escalated else 0,
                    output_preview[:500],
                    now,
                ),
            )
            existing = conn.execute(
                "SELECT * FROM compiled_skills WHERE skill_name = ?", (skill_name,)
            ).fetchone()
            if existing:
                use_count = int(existing["use_count"]) + 1
                success_count = int(existing["success_count"]) + (1 if (success and not escalated) else 0)
                failure_count = int(existing["failure_count"]) + (0 if (success and not escalated) else 1)
                prev_avg = float(existing["avg_code_ms"])
                avg_code_ms = (
                    duration_ms
                    if prev_avg <= 0.0
                    else round((prev_avg * 0.7) + (duration_ms * 0.3), 2)
                )
                shadow_runs = int(existing["shadow_runs"]) + (1 if mode in ("shadow", "code") else 0)
                shadow_successes = int(existing["shadow_successes"]) + (
                    1 if (success and not escalated and mode in ("shadow", "code")) else 0
                )
                avg_prose_ms = float(existing["avg_prose_ms"])
                status = str(existing["status"])

                if escalated or not success:
                    if status == "TRUSTED" and failure_count > success_count:
                        status = "DEGRADED"
                elif status == "TESTED" and shadow_successes >= 1:
                    if avg_code_ms <= avg_prose_ms * shadow_speedup_ratio:
                        status = "TRUSTED"

                b_score = self.compute_bayesian_score(success_count, use_count)
                conn.execute(
                    """
                    UPDATE compiled_skills
                    SET use_count = ?, success_count = ?, failure_count = ?,
                        avg_code_ms = ?, shadow_runs = ?, shadow_successes = ?,
                        bayesian_score = ?, status = ?
                    WHERE skill_name = ?
                    """,
                    (
                        use_count,
                        success_count,
                        failure_count,
                        avg_code_ms,
                        shadow_runs,
                        shadow_successes,
                        b_score,
                        status,
                        skill_name,
                    ),
                )
        return self.get_compiled_skill(skill_name) or {}

    def record_archive_event(
        self,
        event_id: str,
        skill_name: str,
        action: str,
        previous_path: str,
        restored_path: str,
        bayesian_score: float,
        reason: str,
    ) -> Dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO archive_events (
                    event_id, skill_name, action, previous_path, restored_path,
                    bayesian_score, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    skill_name,
                    action,
                    previous_path,
                    restored_path,
                    float(bayesian_score),
                    reason,
                    now,
                ),
            )
        return {
            "event_id": event_id,
            "skill_name": skill_name,
            "action": action,
            "previous_path": previous_path,
            "restored_path": restored_path,
            "bayesian_score": bayesian_score,
            "reason": reason,
            "created_at": now,
        }

    def list_archive_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM archive_events ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_widget(self, widget: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        widget_id = str(widget["widget_id"])
        spec = widget.get("spec")
        if isinstance(spec, (dict, list)):
            spec_str = json.dumps(spec)
        else:
            spec_str = str(widget.get("spec_json", "{}"))

        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM widgets WHERE widget_id = ?", (widget_id,)
            ).fetchone()
            created_at = float(existing["created_at"]) if existing else now
            pinned = int(bool(widget.get("pinned", existing["pinned"] if existing else 0)))
            pin_order = int(widget.get("pin_order", existing["pin_order"] if existing else int(now)))
            note_color = str(widget.get("note_color", existing["note_color"] if existing else "amber"))
            user_notes = str(widget.get("user_notes", existing["user_notes"] if existing else ""))

            conn.execute(
                """
                INSERT INTO widgets (
                    widget_id, session_id, kind, title, subtitle, spec_json,
                    pinned, pin_order, note_color, user_notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(widget_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    kind = excluded.kind,
                    title = excluded.title,
                    subtitle = excluded.subtitle,
                    spec_json = excluded.spec_json,
                    pinned = excluded.pinned,
                    pin_order = excluded.pin_order,
                    note_color = excluded.note_color,
                    user_notes = excluded.user_notes,
                    updated_at = excluded.updated_at
                """,
                (
                    widget_id,
                    str(widget.get("session_id", existing["session_id"] if existing else "")),
                    str(widget.get("kind", existing["kind"] if existing else "chart")),
                    str(widget.get("title", existing["title"] if existing else "Untitled Widget")),
                    str(widget.get("subtitle", existing["subtitle"] if existing else "")),
                    spec_str,
                    pinned,
                    pin_order,
                    note_color,
                    user_notes,
                    created_at,
                    now,
                ),
            )
        return self.get_widget(widget_id) or {}

    def _deserialize_widget_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["pinned"] = bool(item["pinned"])
        try:
            item["spec"] = json.loads(item.get("spec_json") or "{}")
        except Exception:
            item["spec"] = {}
        return item

    def get_widget(self, widget_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM widgets WHERE widget_id = ?", (widget_id,)
            ).fetchone()
            return self._deserialize_widget_row(row) if row else None

    def list_widgets(
        self, session_id: Optional[str] = None, pinned_only: bool = False
    ) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if pinned_only and session_id:
                rows = conn.execute(
                    "SELECT * FROM widgets WHERE pinned = 1 AND (session_id = ? OR session_id = '') ORDER BY pin_order ASC, updated_at DESC",
                    (session_id,),
                ).fetchall()
            elif pinned_only:
                rows = conn.execute(
                    "SELECT * FROM widgets WHERE pinned = 1 ORDER BY pin_order ASC, updated_at DESC"
                ).fetchall()
            elif session_id:
                rows = conn.execute(
                    "SELECT * FROM widgets WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM widgets ORDER BY updated_at DESC"
                ).fetchall()
            return [self._deserialize_widget_row(r) for r in rows]

    def set_widget_pin(
        self,
        widget_id: str,
        pinned: bool,
        note_color: Optional[str] = None,
        user_notes: Optional[str] = None,
        pin_order: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_widget(widget_id)
        if not existing:
            return None
        existing["pinned"] = pinned
        if note_color is not None:
            existing["note_color"] = note_color
        if user_notes is not None:
            existing["user_notes"] = user_notes
        if pin_order is not None:
            existing["pin_order"] = pin_order
        return self.upsert_widget(existing)

    def delete_widget(self, widget_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM widgets WHERE widget_id = ?", (widget_id,))
            return cur.rowcount > 0
