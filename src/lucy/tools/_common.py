"""Helpers shared by tool modules (leading underscore: never treated as a tool module).

The file tools may reach SAFE_ROOT (LUCY_SAFE_ROOT, default the home folder) plus every folder in permissions.yaml
`folders.read` (e.g. whole drives C:/ D:/ E:/). What they may DO there is decided by the safety layer.
"""
import re
from pathlib import Path

from lucy import paths as _paths
from lucy.server import file_guard


def extra_roots() -> list:
    """The `folders.read` entries of permissions.yaml plus the reachable `drives` (a new drive shows up while running),
    as Paths ("c:" -> "c:/", a drive root)."""
    try:
        from lucy import safety                      # lazy: the safety layer imports this module too
        cfg = safety.get().cfg
        folders = list(cfg.read_folders) + [d for d in cfg.drive_roots() if d not in cfg.read_folders]
    except Exception:                                # noqa: BLE001 - no safety layer -> SAFE_ROOT only
        return []
    return [Path(f + "/") if re.fullmatch(r"[A-Za-z]:", f) else Path(f) for f in folders]


def resolve_path(file_path: str):
    """Resolve what the model typed to a real path (None if outside every allowed root). See file_guard.resolve_path.
    SAFE_ROOT is tried first, so relative paths and guesses behave exactly as before."""
    first = file_guard.resolve_path(file_path, _paths.SAFE_ROOT, _paths.ROOT)
    if first is not None and first.exists():
        return first
    for root in extra_roots():                       # an existing file in another allowed root beats a guess
        r = file_guard.resolve_path(file_path, root, _paths.ROOT)
        if r is not None and (r.exists() or first is None):
            return r
    return first


def outside_root(file_path: str) -> str:
    msg = file_guard.outside_root_message(file_path, _paths.SAFE_ROOT, _paths.ROOT)
    roots = [p.as_posix() for p in extra_roots()]
    return msg + (f" Other allowed locations: {', '.join(roots)}." if roots else "")


def allowed_roots_text() -> str:
    """Where the file tools can work, for tool descriptions and the prompt: e.g. "C:/, D:/, E:/"."""
    out = []
    for p in [_paths.SAFE_ROOT] + extra_roots():
        t = Path(p).as_posix()
        if re.fullmatch(r"[A-Za-z]:/?", t):
            t = t[0].upper() + ":/"
        if not any(t.lower().startswith(o.lower().rstrip("/") + "/") or t.lower() == o.lower() for o in out):
            out = [o for o in out if not o.lower().startswith(t.lower().rstrip("/") + "/")] + [t]
    return ", ".join(out)


def safety_hint() -> str:
    """One short sentence the model needs so it does not invent restrictions or ignore real ones."""
    return ("Some locations ask the user first (approval card) and secrets/system folders are blocked; "
            "if a call is refused or denied, the result says why.")
