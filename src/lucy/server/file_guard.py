"""Safe, forgiving file editing for the agent's file tools.

What goes wrong when a small model edits files, and what this module does about it:

  * wrong path          the model guesses ("Lucy_Core/src/x.py", "/Lucy/Lucy_Core/...", "src\\x.py")
                        -> resolve_path() tries the project root, the file-tool root and the usual guesses
  * CRLF vs LF          Windows files have \\r\\n, the model types \\n  -> edits ignore the difference and the
                        file keeps its own line endings
  * stray whitespace    trailing spaces / blank lines around old_string -> tolerated when the match is unique
  * old_string slightly wrong -> the error shows the closest real lines, so the retry can copy them
  * a broken edit       -> .py/.json files are validated BEFORE saving; invalid content is refused
  * a bad edit anyway   -> every change to a file under the project root is backed up first
                           (runtime/backups/<path>/<timestamp>.bak, newest 20 kept)
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import time
from pathlib import Path


# ----------------------------------------------------------------------------------- paths
def resolve_path(file_path: str, safe_root: Path, project_root: Path) -> Path | None:
    """Resolve what the model typed to a real path inside safe_root, or None if there isn't one.

    Absolute paths are used as given. Relative / guessed paths are tried against the project root
    (Lucy_Core) first, then the file-tool root. The first candidate that exists wins; if none exists
    (e.g. a file about to be created) the first candidate inside safe_root is returned.
    """
    raw = str(file_path or "").strip().strip("\"'`")
    if not raw:
        return None
    p = raw.replace("\\", "/")
    cands: list[Path] = []
    if p.startswith("~"):
        cands.append(Path(p).expanduser())
    elif len(p) > 1 and p[1] == ":":                                  # C:/...
        cands.append(Path(p))
    elif p.startswith("/"):
        cands.append(Path(p))                                         # real POSIX path
        stripped = p.lstrip("/")
        cands += [safe_root / stripped, project_root / stripped, project_root.parent / stripped]
    else:
        cands += [project_root / p, safe_root / p]
    name = project_root.name
    rel = p.lstrip("/")
    if rel.startswith(name + "/"):                                    # "Lucy_Core/src/..."
        cands += [project_root / rel[len(name) + 1:], project_root.parent / rel]
    if rel.startswith("Lucy/" + name + "/"):                          # "Lucy/Lucy_Core/src/..."
        cands.append(project_root / rel[len("Lucy/" + name) + 1:])

    inside: list[Path] = []
    for c in cands:
        try:
            t = c.resolve()
            t.relative_to(safe_root)
        except (OSError, ValueError):
            continue
        if t.exists():
            return t
        inside.append(t)
    return inside[0] if inside else None


def outside_root_message(file_path: str, safe_root: Path, project_root: Path) -> str:
    return (f"Error: '{file_path}' is outside the allowed root ({safe_root.as_posix()}). Lucy Core itself is at "
            f"{project_root.as_posix()} - use absolute paths such as {project_root.as_posix()}/src/lucy/server/api.py "
            f"or paths relative to Lucy Core such as src/lucy/server/api.py.")


# ----------------------------------------------------------------------------------- edits
def _norm(s: str) -> str:
    return s.replace("\r\n", "\n")


def apply_edit(text: str, old: str, new: str, replace_all: bool = False) -> tuple[str | None, str]:
    """Replace `old` with `new` in `text` (both with \\n newlines). Returns (new_text, message) or
    (None, error_message). Exact match first; then a match that ignores trailing whitespace per line."""
    old, new = _norm(old), _norm(new)
    if old.strip() == "":
        return None, ("Error: 'old_string' is empty or only whitespace. To add text, use the lines around the "
                      "insertion point as old_string (and repeat them in new_string).")
    n = text.count(old)
    if n == 1 or (n > 1 and replace_all):
        return (text.replace(old, new) if replace_all else text.replace(old, new, 1)), f"Replaced {n if replace_all else 1} occurrence(s)"
    if n > 1:
        lines = _match_lines(text, old)
        return None, (f"Error: 'old_string' matches {n} times (at lines {', '.join(map(str, lines[:8]))}). "
                      "Add more surrounding lines to make it unique, or use replace_all=true.")

    # --- tier 2: line-based match, ignoring trailing whitespace and stray blank lines at the edges
    tl, ol, nl = text.split("\n"), old.split("\n"), new.split("\n")
    trimmed = False
    while ol and ol[0].strip() == "":
        ol.pop(0); trimmed = True
    while ol and ol[-1].strip() == "":
        ol.pop(); trimmed = True
    if trimmed:
        while nl and nl[0].strip() == "":
            nl.pop(0)
        while nl and nl[-1].strip() == "":
            nl.pop()
    want = [l.rstrip() for l in ol]
    spans = [i for i in range(len(tl) - len(ol) + 1) if all(tl[i + k].rstrip() == want[k] for k in range(len(ol)))]
    if len(spans) == 1 or (spans and replace_all):
        for i in reversed(spans if replace_all else spans[:1]):
            tl[i:i + len(ol)] = nl
        return "\n".join(tl), f"Replaced {len(spans) if replace_all else 1} occurrence(s) (ignoring whitespace differences)"
    if len(spans) > 1:
        return None, (f"Error: 'old_string' matches {len(spans)} times ignoring whitespace (lines "
                      f"{', '.join(str(i + 1) for i in spans[:8])}). Add more surrounding lines or use replace_all=true.")
    return None, "Error: 'old_string' not found in file." + _closest_hint(tl, ol)


def _match_lines(text: str, old: str) -> list[int]:
    out, start = [], 0
    while (i := text.find(old, start)) != -1:
        out.append(text.count("\n", 0, i) + 1)
        start = i + max(1, len(old))
    return out


def _closest_hint(tl: list[str], ol: list[str]) -> str:
    """Show the real lines that most resemble old_string so the model can copy them exactly."""
    if not ol or not tl or len(tl) > 20000:
        return " Read the file again with read_local_file and copy old_string exactly."
    size, best, at = min(len(ol), 40), 0.0, -1
    first = ol[0].strip()
    for i in range(len(tl) - size + 1):
        if first and difflib.SequenceMatcher(None, tl[i].strip(), first).ratio() < 0.5:
            continue
        r = difflib.SequenceMatcher(None, "\n".join(l.strip() for l in tl[i:i + size]),
                                    "\n".join(l.strip() for l in ol[:size])).ratio()
        if r > best:
            best, at = r, i
    if at < 0 or best < 0.45:
        return " Nothing similar found; read the file again with read_local_file and copy old_string exactly."
    shown = "\n".join(f"{at + k + 1}: {tl[at + k]}" for k in range(min(size, 12)))
    return (f" Closest match is at line {at + 1} ({int(best * 100)}% similar). The file actually contains:\n{shown}\n"
            "Copy old_string exactly from the file (indentation included).")


# ----------------------------------------------------------------------------------- validation
def validate_content(path: Path, text: str) -> str | None:
    """Return an error message if `text` would be an invalid .py / .json file, else None."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            compile(text, str(path), "exec")
        elif suffix == ".json":
            json.loads(text)
        elif suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                return None
            yaml.safe_load(text)
    except SyntaxError as e:
        return (f"Refused: this change would leave {path.name} with a Python syntax error at line {e.lineno}: "
                f"{e.msg}. Nothing was changed. Fix the code in new_string and try again.")
    except Exception as e:                                           # noqa: BLE001 - json / yaml errors
        return f"Refused: this change would leave {path.name} as invalid {suffix[1:].upper()} ({e}). Nothing was changed."
    return None


# ----------------------------------------------------------------------------------- writing
def backup_file(path: Path, project_root: Path, keep: int = 20) -> Path | None:
    """Copy `path` to runtime/backups/ before it is changed (only for files inside the project)."""
    try:
        rel = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    if not path.is_file() or rel.parts[:2] == ("runtime", "backups"):
        return None
    folder = project_root / "runtime" / "backups" / "__".join(rel.parts)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{time.strftime('%Y%m%d-%H%M%S')}.bak"
        n = 1
        while dest.exists():
            dest = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}.bak"
            n += 1
        shutil.copy2(path, dest)
        for old in sorted(folder.glob("*.bak"))[:-keep]:
            old.unlink(missing_ok=True)
        return dest
    except OSError:
        return None


def read_text_keep_style(path: Path) -> tuple[str, bool]:
    """-> (text with \\n newlines, file_uses_crlf). Raises ValueError for non-UTF-8 files."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{path.name} is not valid UTF-8 ({e}); refusing to edit it as text") from e
    return text.replace("\r\n", "\n"), b"\r\n" in raw


def write_text_atomic(path: Path, text: str, crlf: bool) -> None:
    data = (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-lucy")
    tmp.write_bytes(data)
    os.replace(tmp, path)                                            # never leaves a half-written file


def edit_file(path: Path, old: str, new: str, replace_all: bool, project_root: Path) -> str:
    """Full edit pipeline. Returns a success message or an 'Error:/Refused:' string."""
    if not path.exists() or not path.is_file():
        return f"File not found: {path.as_posix()}"
    try:
        text, crlf = read_text_keep_style(path)
    except (OSError, ValueError) as e:
        return f"Error: {e}"
    new_text, msg = apply_edit(text, old, new, replace_all)
    if new_text is None:
        return f"{msg} (file: {path.as_posix()})"
    if new_text == text:
        return f"No change: new_string is identical to old_string in {path.as_posix()}"
    problem = validate_content(path, new_text)
    if problem:
        return problem
    bak = backup_file(path, project_root)
    try:
        write_text_atomic(path, new_text, crlf)
    except OSError as e:
        return f"Error: could not write {path.as_posix()}: {e}"
    extra = ""
    try:
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
        if bak:
            extra = f" Backup saved ({bak.name})."
        if rel.startswith("src/") and path.suffix == ".py":
            extra += " This is Lucy Core code: the change takes effect after Lucy Core restarts."
        elif rel.startswith(".core/personas/"):
            extra += " Persona changes take effect after Lucy Core restarts."
    except ValueError:
        pass
    return f"{msg} in {path.as_posix()}.{extra}"


def write_file(path: Path, content: str, project_root: Path) -> str:
    """write_local_file pipeline: validate, back up an existing file, keep its line endings."""
    content = _norm(content)
    problem = validate_content(path, content)
    if problem:
        return problem
    crlf = os.linesep == "\r\n"
    existed = path.is_file()
    if existed:
        try:
            _, crlf = read_text_keep_style(path)
        except (OSError, ValueError):
            pass
    bak = backup_file(path, project_root) if existed else None
    try:
        write_text_atomic(path, content, crlf)
    except OSError as e:
        return f"Error: could not write {path.as_posix()}: {e}"
    return (f"Written {len(content)} characters to {path.as_posix()}."
            + (f" Previous version backed up ({bak.name})." if bak else ""))


# ----------------------------------------------------------------------------------- "did you mean"
_SKIP_DIRS = {".git", "node_modules", "__pycache__", "backups", "models", ".venv", "venv", "cache", "tmp"}


def suggest_paths(file_path: str, project_root: Path, limit: int = 5, max_files: int = 8000) -> list[str]:
    """Files in the project whose name is the same as / close to what the model typed.

    A wrong path is the most common reason a read/edit/send fails; naming the real file lets the model fix it
    in the next call instead of giving up."""
    name = Path(str(file_path).replace("\\", "/")).name.lower()
    if not name:
        return []
    scored, seen = [], 0
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            seen += 1
            if seen > max_files:
                break
            low = f.lower()
            score = 1.0 if low == name else difflib.SequenceMatcher(None, low, name).ratio()
            if score >= 0.72:
                try:
                    rel = (Path(root) / f).relative_to(project_root).as_posix()
                except ValueError:
                    continue
                scored.append((score, rel))
        if seen > max_files:
            break
    scored.sort(key=lambda t: (-t[0], len(t[1])))
    return [rel for _s, rel in scored[:limit]]


def not_found_message(file_path: str, project_root: Path) -> str:
    near = suggest_paths(file_path, project_root)
    base = f"File not found: {file_path}"
    if near:
        return base + f". Did you mean: {', '.join(near)}?"
    return base + ". Use find_file or list_local_files to locate it."
