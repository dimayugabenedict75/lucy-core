---
name: skill-refinement-loop
description: Continuously improve skills based on real usage feedback.
triggers: refine skill, update skill, improve skill, lint skill, validate skill
category: process
---

## When to Use

- User reports a skill is producing wrong/missing tools, stale info, or broken triggers
- A skill trigger regex fails to match expected messages
- Skill instructions are too verbose, outdated, or missing key steps
- The `/api/skills/validate` endpoint reports issues for a skill (name format, description length, invalid regex, overlaps)
- Frontmatter fields are missing (version, author, license) or names don't match slugs

## Procedure

1. **Read the skill file** with `read_local_file` — always read before writing, never patch from memory
2. **Run validation** — check `/api/skills/validate` and `/api/skills/test-trigger` to identify concrete issues
3. **Check frontmatter** — verify `skill_id` matches filename stem, `name` is lowercase-with-hyphens, `description` ≤ 60 chars and ends with a period
4. **Verify trigger syntax** — if trigger is a regex, test it compiles: `re.search(trigger, sample_message)`; if it fails to compile, add keyword fallback
5. **Compare expected vs actual** — run the skill's described action against what the agent actually did
6. **Patch with `edit_local_file`** — make targeted edits, not full rewrites, unless the skill is fundamentally broken
7. **Re-validate** — re-run `/api/skills/validate` and `/api/skills/test-trigger` to confirm the fix

## Pitfalls

- **Frontmatter name/dir mismatch**: The linter enforces that `name` in YAML frontmatter matches the directory filename. Keep them identical (e.g. `name: shell-operations` in `shell-operations.md`).
- **Description over 60 chars**: The skill index truncates at 60 chars; longer descriptions lose routing signal. Keep one sentence.
- **Regex trigger that doesn't compile**: `re.compile(trigger)` raises `re.error` — the agent silently falls through to keyword matching. Always test trigger regexes with `re.search`.
- **Keyword fallback too broad**: Trigger keywords split on whitespace grab partial words. A trigger like `port` matches `import`, `support`, `portable`. Use word-boundary regexes when possible.
- **Stale `__pycache__`**: After editing a skill .md file, the SkillsManager's mtime cache should auto-reload. If not, clear `__pycache__` in `src/lucy/`.
- **CRLF line endings**: Windows editors may save with CRLF, which breaks `content.replace()` string matching in patches. Use `re.sub` with `\
?\\n` patterns or write the whole file.