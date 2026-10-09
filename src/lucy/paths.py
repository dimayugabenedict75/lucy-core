"""Central filesystem locations for Lucy Core.

Everything is derived from where this file actually lives, so the project can
be moved or run under a different Windows user without editing code.

Optional environment overrides:
    LUCY_ROOT       project root            (default: folder containing src/)
    LUCY_SAFE_ROOT  file-tool sandbox root  (default: the user's home folder)
    HERMES_HOME     Hermes data folder used by the TTS wrapper
                    (default: <home>/AppData/Local/hermes)
"""

import os
from pathlib import Path

# <ROOT>/src/lucy/paths.py  ->  parents[2] == <ROOT>
ROOT = Path(os.environ.get("LUCY_ROOT") or Path(__file__).resolve().parents[2]).resolve()
SRC = ROOT / "src"
RUNTIME = ROOT / "runtime"
UI_DIR = ROOT / "ui"
PERSONA_PATH = ROOT / ".core" / "personas" / "lucy.md"

# Sandbox root for the file tools and /api/file.
SAFE_ROOT = Path(os.environ.get("LUCY_SAFE_ROOT") or Path.home()).resolve()

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / "AppData" / "Local" / "hermes"))
