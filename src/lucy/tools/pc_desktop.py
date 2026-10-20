"""P5 non-admin PC tools: list/focus windows, clipboard, notifications, volume, open an app/file/URL, take a
screenshot, and click a control by its accessible name (UI Automation, preferred over clicking at pixel guesses).

None of these need administrator rights, so they never go to the broker. They are `system` tier (control the PC), so
they ask in the default "Ask for risky" mode; reading (window list, clipboard get) is downgraded to read in policy.py.
cua_mouse / cua_keyboard / cua_cursor (raw pixel control) still live in desktop.py as a last resort.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from lucy import paths as _paths
from . import _pc

S = {"type": "string"}


def _h_window(args, ctx):
    a = _pc.action_of(args, "list")
    if a == "list":
        return _pc.execute("window.list", args, tool="window")
    if a in ("focus", "minimize", "maximize", "restore"):
        return _pc.execute("window.control", {**args, "control": a}, tool="window", dry_run=args.get("dry_run") is True)
    return "Failed: action must be list, focus, minimize, maximize or restore"


def _h_desktop(args, ctx):
    a = _pc.action_of(args)
    dry = args.get("dry_run") is True
    if a == "clipboard_get":
        return _pc.execute("desktop.clipboard_get", args, tool="desktop")
    if a == "clipboard_set":
        return _pc.execute("desktop.clipboard_set", args, tool="desktop", dry_run=dry)
    if a == "notify":
        return _pc.execute("desktop.notify", args, tool="desktop", dry_run=dry)
    if a == "volume":
        return _pc.execute("desktop.volume", args, tool="desktop", dry_run=dry)
    if a in ("mute", "unmute"):
        return _pc.execute("desktop.volume", {**args, "control": a}, tool="desktop", dry_run=dry)
    if a == "open":
        return _pc.execute("desktop.open", args, tool="desktop", dry_run=dry)
    return "Failed: action must be clipboard_get, clipboard_set, notify, volume, mute, unmute or open"


def _tmp_png() -> Path:
    d = _paths.RUNTIME / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"shot-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}.png"


def _h_screenshot(args, ctx):
    if args.get("dry_run") is True:
        return "DRY RUN: would take a screenshot."
    out = _tmp_png()
    res = _pc.execute("desktop.screenshot", {**args, "out_path": str(out)}, tool="screenshot")
    if not res.startswith(str(out)):
        return res                                             # an error (or dry-run text)
    return (f"MEDIA:{out}\nScreenshot saved and shown. To read what is on screen before clicking, call analyze_image "
            f"with image_path '{out}'.")


def _h_ui_click(args, ctx):
    """Click a control by its accessible NAME (button/link/menu text) using UI Automation, not a pixel guess."""
    name = str(args.get("name") or "").strip()
    if not name:
        return "Failed: 'name' is required (the visible text of the button/link/menu item to click)"
    try:
        import comtypes  # noqa: F401
        from pywinauto import Desktop
    except ImportError:
        return ("Failed: clicking a control by name needs pywinauto (pip install pywinauto). Fallbacks: take a "
                "screenshot, find the target with analyze_image, then use cua_mouse to click its coordinates.")
    if args.get("dry_run") is True:
        return f"DRY RUN: would click the control named '{name}'."
    control_type = str(args.get("control_type") or "").strip()
    window_title = str(args.get("window") or "").strip()
    try:
        desk = Desktop(backend="uia")
        root = desk.window(title_re=f".*{window_title}.*") if window_title else desk
        kw = {"title": name}
        if control_type:
            kw["control_type"] = control_type
        el = root.child_window(**kw)
        el.wait("exists ready", timeout=5)
        el.click_input()
        return f"Done: clicked '{name}'."
    except Exception as e:                                      # noqa: BLE001 - pywinauto raises many types
        return (f"Failed: could not find/click a control named '{name}' ({type(e).__name__}: {str(e)[:200]}). "
                "Try window=<part of the window title>, a different name, or the screenshot + cua_mouse fallback.")


def describe_call(name: str, args: dict) -> str:
    a = _pc.action_of(args)
    dry = " (dry run)" if args.get("dry_run") is True else ""
    if name == "window":
        return f"Window: {a} {args.get('title') or args.get('pid') or ''}{dry}"
    if name == "desktop":
        if a == "open":
            return f"Open {args.get('target') or args.get('path') or args.get('url')}{dry}"
        if a == "clipboard_set":
            return f"Copy {len(str(args.get('text') or ''))} characters to the clipboard{dry}"
        if a == "notify":
            return f"Show a notification: {args.get('title', 'Lucy')} - {str(args.get('message') or args.get('text'))[:80]}{dry}"
        if a == "volume":
            return f"Set the volume to {args.get('level')}%{dry}"
        return f"Desktop: {a}{dry}"
    if name == "ui_click":
        return f"Click the on-screen control named '{args.get('name')}'{dry}"
    return ""


TOOLS = [
    {"schema": _pc.schema("window", "Open windows: list them, or focus / minimize / maximize / restore one by pid "
                          "(from list) or part of its title.", ["list", "focus", "minimize", "maximize", "restore"],
                          {"pid": {"type": "integer"}, "title": {"type": "string", "description": "part of the window title"},
                           "filter": {"type": "string", "description": "list: part of a title or program name"}}),
     "handler": _h_window, "tier": "system", "label": "window"},
    {"schema": _pc.schema("desktop", "Everyday desktop actions: read or set the clipboard, show a notification, set or "
                          "mute the volume, or open an app/file/URL.",
                          ["clipboard_get", "clipboard_set", "notify", "volume", "mute", "unmute", "open"],
                          {"text": {"type": "string", "description": "clipboard_set: text to copy"},
                           "title": {"type": "string", "description": "notify: heading"},
                           "message": {"type": "string", "description": "notify: body"},
                           "level": {"type": "integer", "description": "volume: 0-100"},
                           "target": {"type": "string", "description": "open: app name, file path or http(s) URL"}}),
     "handler": _h_desktop, "tier": "system", "label": "desktop"},
    {"schema": {"type": "function", "function": {
        "name": "screenshot",
        "description": "Take a screenshot (whole screen, or a region) and show it. Then use analyze_image on it to read "
                       "what is on screen before clicking anything.",
        "parameters": {"type": "object", "properties": {
            "region": {"type": "object", "description": "optional {x, y, w, h} in pixels",
                       "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                      "w": {"type": "integer"}, "h": {"type": "integer"}}},
            "dry_run": {"type": "boolean"}}}}},
     "handler": _h_screenshot, "tier": "system", "label": "screenshot"},
    {"schema": {"type": "function", "function": {
        "name": "ui_click",
        "description": "Click an on-screen control by its visible text (button, link, menu item) via Windows UI "
                       "Automation. Steadier than clicking coordinates. Needs pywinauto.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "the control's visible text, e.g. Save"},
            "control_type": {"type": "string", "description": "optional: Button, MenuItem, Hyperlink, ..."},
            "window": {"type": "string", "description": "optional: part of the window title to search inside"},
            "dry_run": {"type": "boolean"}}, "required": ["name"]}}},
     "handler": _h_ui_click, "tier": "system", "label": "click"},
]
