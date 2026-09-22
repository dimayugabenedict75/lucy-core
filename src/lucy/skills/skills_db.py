"""
Skills Database for Lucy Core.

Stores skills in the same SQLite database as sessions (memory.sqlite pattern).
Two-layer cache: in-memory dict + DB persistence.
  - INSERT on learn (or import from files)
  - SELECT on query (by name or by semantic trigger match)
"""

import sqlite3
import threading
import re
import yaml
from pathlib import Path
from typing import Optional

DB_PATH = "C:/Users/dimay/Lucy/Lucy_Core/runtime/sessions.sqlite"
SKILLS_DIR = Path("C:/Users/dimay/Lucy/Lucy_Core/src/lucy/skills/definitions")


class SkillsDatabase:
    """Database-backed skill storage with in-memory cache."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._cache: dict[str, dict] = {}  # skill_name -> skill dict (layer 1 cache)
        self._trigger_index: dict[str, list[str]] = {}  # token -> list of skill names
        self._initialized = False

    def _ensure_tables(self):
        if self._initialized:
            return
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    skill_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT UNIQUE NOT NULL,
                    trigger    TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    tools      TEXT DEFAULT '[]',
                    category   TEXT DEFAULT 'general',
                    source     TEXT DEFAULT 'learned',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name)
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_skills_trigger ON skills(trigger)
            """)
            self._conn.commit()
            self._initialized = True

    def load_all_to_cache(self) -> int:
        """Load all skills from DB into the in-memory cache. Returns count."""
        self._ensure_tables()
        with self._lock:
            rows = self._conn.execute(
                "SELECT skill_id, name, trigger, instructions, tools, category, source FROM skills"
            ).fetchall()
        for row in rows:
            skill = dict(row)
            self._cache[skill["name"]] = skill
        self._build_trigger_index()
        return len(rows)

    def _build_trigger_index(self):
        """Rebuild the token -> skill_names index from the cache."""
        self._trigger_index = {}
        for name, skill in self._cache.items():
            triggers = skill.get("trigger", "")
            # Split triggers on comma, pipe, or whitespace
            tokens = re.split(r'[\s,|]+', triggers.lower())
            for token in tokens:
                if len(token) > 3:
                    self._trigger_index.setdefault(token, []).append(name)

    def get_by_name(self, name: str) -> Optional[dict]:
        """Get a skill by name from cache, with tools parsed to list."""
        skill = self._cache.get(name)
        if skill is None:
            return None
        result = dict(skill)
        tools_raw = result.get("tools", "[]")
        if isinstance(tools_raw, str):
            try:
                result["tools"] = yaml.safe_load(tools_raw) if tools_raw else []
            except Exception:
                result["tools"] = []
        elif isinstance(tools_raw, list):
            result["tools"] = tools_raw
        else:
            result["tools"] = []
        return result

    def learn_skill(
        self,
        name: str,
        trigger: str,
        instructions: str,
        tools: Optional[list] = None,
        category: str = "general",
        source: str = "learned",
    ) -> dict:
        """INSERT a new skill. Updates if name exists (upsert)."""
        self._ensure_tables()
        tools_json = "[]" if tools is None else yaml.dump(tools, default_flow_style=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO skills (name, trigger, instructions, tools, category, source) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "trigger = excluded.trigger, "
                "instructions = excluded.instructions, "
                "tools = excluded.tools, "
                "category = excluded.category, "
                "source = excluded.source, "
                "updated_at = CURRENT_TIMESTAMP",
                (name, trigger, instructions, tools_json, category, source),
            )
            self._conn.commit()
        # Update cache — store tools as list, not YAML string
        tools_list = tools if tools is not None else []
        skill = {
            "name": name,
            "trigger": trigger,
            "instructions": instructions,
            "tools": tools_list,
            "category": category,
            "source": source,
        }
        self._cache[name] = skill
        self._build_trigger_index()
        return skill

    def get_relevant_skills(self, query: str, top_k: int = 5) -> list[dict]:
        """Return skills whose triggers match tokens in the query, sorted by relevance."""
        query_lower = query.lower()
        query_tokens = [t for t in re.split(r'[\s,|]+', query_lower) if len(t) > 3]

        scores: dict[str, int] = {}
        for token in query_tokens:
            for skill_name in self._trigger_index.get(token, []):
                scores[skill_name] = scores.get(skill_name, 0) + 1

        if not scores:
            return []

        # Sort by score desc, then by name for determinism
        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        results = []
        for skill_name, _ in ranked[:top_k]:
            skill = self.get_by_name(skill_name)  # parsed copy
            if skill:
                results.append(skill)
        return results

    def list_all(self) -> list[dict]:
        """Return all cached skills (summary fields only)."""
        result = []
        for s in self._cache.values():
            tools_raw = s.get("tools", "[]")
            try:
                tools_parsed = yaml.safe_load(tools_raw) if tools_raw else []
            except Exception:
                tools_parsed = []
            result.append({
                "name": s["name"],
                "trigger": s["trigger"],
                "category": s["category"],
                "tools": tools_parsed if isinstance(tools_parsed, list) else [],
                "source": s["source"],
            })
        return result

    def import_from_files(self, source_dir: Path = SKILLS_DIR) -> int:
        """Import all .md skill files into the DB. Uses front-matter parsing."""
        if not source_dir.exists():
            return 0
        count = 0
        for file_path in source_dir.glob("*.md"):
            content = file_path.read_text(encoding="utf-8")
            parts = content.split('---', 2)
            if len(parts) < 3:
                continue
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                continue
            body = parts[2].strip()
            name = metadata.get('name') or file_path.stem
            triggers = metadata.get('trigger', metadata.get('triggers', []))
            if isinstance(triggers, list):
                trigger = ', '.join(triggers)
            else:
                trigger = str(triggers)
            tools = metadata.get('tools', [])
            category = metadata.get('category', 'general')
            instructions = body or metadata.get('instructions', '')
            self.learn_skill(
                name=name,
                trigger=trigger,
                instructions=instructions,
                tools=tools,
                category=category,
                source="imported",
            )
            count += 1
        return count

    def close(self):
        self._conn.close()
