from pathlib import Path
import yaml
import re
from typing import Optional
from lucy.skills.skills_db import SkillsDatabase

class SkillsManager:
    """Manages skills with DB-backed storage and in-memory caching (two-layer cache)."""

    def __init__(self, db_path: str = None, definitions_dir: str = None):
        self._db = SkillsDatabase(db_path) if db_path else SkillsDatabase()

        if definitions_dir:
            self.skills_dir = Path(definitions_dir)
        else:
            self.skills_dir = Path(__file__).resolve().parent / "definitions"

        # Load all DB skills into cache
        loaded = self._db.load_all_to_cache()

        # If cache is empty, try importing from .md files
        if loaded == 0:
            self._db.import_from_files(self.skills_dir)
            loaded = self._db.load_all_to_cache()

        self.skills = self._db.list_all()

    def _parse_skill_file(self, content: str):
        """Parses a markdown file with YAML front-matter (legacy compat)."""
        parts = content.split('---', 2)
        if len(parts) < 3:
            return {}, content.strip()
        try:
            metadata = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            metadata = {}
        body = parts[2].strip()
        return metadata, body

    def _load_skill_from_file(self, file_path: Path) -> dict | None:
        """Legacy: parse a .md skill file (still useful for export)."""
        try:
            content = file_path.read_text(encoding="utf-8")
            metadata, body = self._parse_skill_file(content)
            name = metadata.get('name') or file_path.stem
            skill = {
                'name': name,
                'description': metadata.get('description', ''),
                'triggers': metadata.get('triggers', metadata.get('trigger', [])),
                'instructions': body or metadata.get('instructions', ''),
                'tools': metadata.get('tools', []),
                'category': metadata.get('category', 'general'),
                'source_file': str(file_path)
            }
            return skill
        except Exception as e:
            print(f"Error loading skill {file_path}: {e}")
            return None

    def _load_from_directory(self, directory: Path):
        """Legacy: load .md files. Still used if needed."""
        if not directory.exists():
            return
        for file_path in directory.glob("*.md"):
            skill = self._load_skill_from_file(file_path)
            if skill:
                trigger = skill.get('triggers', [])
                if isinstance(trigger, list):
                    trigger = ', '.join(trigger)
                self._db.learn_skill(
                    name=skill['name'],
                    trigger=trigger,
                    instructions=skill['instructions'],
                    tools=skill.get('tools', []),
                    category=skill['category'],
                    source="imported",
                )

    def _build_trigger_index(self):
        """Rebuild the token -> skill_names index from the cache."""
        self._trigger_index = {}
        for name, skill in self._cache.items():
            triggers = skill.get("trigger", "")
            tokens = re.split(r'[\s,|]+', triggers.lower())
            for token in tokens:
                if len(token) > 3:
                    self._trigger_index.setdefault(token, []).append(name)

    def _deduplicate_by_name(self):
        """DB enforces UNIQUE on name, so dedup is automatic."""
        pass

    def refresh_cache(self) -> list[dict]:
        """Refresh the primary skills list from the database cache."""
        self.skills = self._db.list_all()
        return self.skills

    def list_skills(self) -> list[dict]:
        """Return the current set of skills from the cache."""
        return self.skills

    def get_skill_by_name(self, name: str) -> Optional[dict]:
        return self._db.get_by_name(name)

    def get_relevant_skills(self, query: str, top_k: int = 5) -> list[dict]:
        return self._db.get_relevant_skills(query, top_k)

    def learn_skill(self, name: str, trigger: str, instructions: str, tools: list = None,
                     category: str = "general", source: str = "learned") -> dict:
        """Learn a new skill — INSERT into DB, update cache."""
        skill = self._db.learn_skill(name, trigger, instructions, tools, category, source)
        self.refresh_cache()
        return skill

    def export_to_lucy_core(self, dest_dir: str = None):
        """Exports all skills to the internal Lucy Core definitions folder as .md."""
        dest = Path(dest_dir) if dest_dir else self.skills_dir
        dest.mkdir(parents=True, exist_ok=True)
        for skill in self._db._cache.values():
            triggers = skill.get("trigger", "")
            content = (
                f"---\n"
                f"name: {skill['name']}\n"
                f"description: {skill.get('category', 'general')}\n"
                f"triggers: {triggers}\n"
                f"category: {skill.get('category', 'general')}\n"
                f"---\n\n"
                f"{skill['instructions']}"
            )
            (dest / f"{skill['name']}.md").write_text(content, encoding="utf-8")
