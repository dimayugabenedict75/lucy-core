"""Sticker picker backend: lists and serves the user's stickers.

Only PNG files directly inside  <project>/assets/stickers/User/  are exposed.
The Light/ and Dark/ folders next to it are deliberately NOT reachable from here.

Wire it up in api.py with two lines (next to the voice router):

    from lucy.server.stickers import router as stickers_router
    app.include_router(stickers_router, dependencies=[Depends(verify_api_key)])

Override the folder with the LUCY_STICKER_DIR environment variable if needed.
"""

import os
import re
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from lucy.paths import ROOT

STICKER_DIR = Path(os.environ.get("LUCY_STICKER_DIR") or (ROOT / "assets" / "stickers" / "User"))

router = APIRouter(prefix="/api/stickers", tags=["stickers"])

# Names that are safe to put in a URL and in the "[sticker: name.png]" chat token.
_SAFE_NAME = re.compile(r"^[^\[\]\\/:*?\"<>|\r\n]+$")


def _natural_key(name: str):
    # 2.png before 10.png
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def list_sticker_names() -> List[str]:
    """PNG files directly inside STICKER_DIR (no subfolders, no hidden files)."""
    try:
        entries = list(STICKER_DIR.iterdir())
    except OSError:
        return []
    names = [
        p.name
        for p in entries
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() == ".png"
        and _SAFE_NAME.match(p.name)
    ]
    return sorted(names, key=_natural_key)


@router.get("")
def list_stickers():
    names = list_sticker_names()
    return {
        "folder": "User",
        "stickers": [{"name": n, "url": f"/api/stickers/User/{n}"} for n in names],
    }


@router.get("/User/{filename}")
def get_sticker(filename: str):
    # Serve only a name that is in the listing above. That rules out ../ tricks,
    # other folders and any non-PNG, without having to reason about paths.
    if filename not in list_sticker_names():
        raise HTTPException(status_code=404, detail="Sticker not found")
    return FileResponse(
        STICKER_DIR / filename,
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},  # revalidate (ETag), so a replaced file shows up
    )
