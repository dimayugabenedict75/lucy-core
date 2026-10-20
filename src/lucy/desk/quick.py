"""Item 12 - global hotkey quick-entry (Windows, optional). Run with:  pythonw -m lucy.desk.quick   (or quick.bat)

Press Ctrl+Alt+Space anywhere: a tiny always-on-top box appears; Enter opens Lucy's chat with that text as a NEW message
(the chat page reads ?q=). Esc closes the box. Uses only ctypes + tkinter (no extra packages). If `pystray` and `Pillow`
are installed a tray icon (Open Lucy / Quick entry / Quit) is added too. Settings via env: LUCY_URL (default
http://127.0.0.1:8090), LUCY_QUICK_KEY (default ctrl+alt+space).
"""

from __future__ import annotations

import os
import sys
import threading
import urllib.parse
import webbrowser

MOD = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
VK = {"space": 0x20, "enter": 0x0D, "tab": 0x09}


def base_url() -> str:
    return os.environ.get("LUCY_URL", "http://127.0.0.1:8090").rstrip("/")


def quick_url(text: str, base: str | None = None) -> str:
    q = " ".join((text or "").split()) if len((text or "").splitlines()) <= 1 else (text or "").strip()
    return f"{(base or base_url())}/?q={urllib.parse.quote(q)}" if q else f"{(base or base_url())}/"


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modifiers, virtual-key code). Raises ValueError for anything it does not know."""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("a hotkey needs a modifier and a key, like ctrl+alt+space")
    mods = 0
    for p in parts[:-1]:
        if p not in MOD:
            raise ValueError(f"unknown modifier {p!r}")
        mods |= MOD[p]
    key = parts[-1]
    if key in VK:
        return mods, VK[key]
    if len(key) == 1 and key.isalnum():
        return mods, ord(key.upper())
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        return mods, 0x70 + int(key[1:]) - 1
    raise ValueError(f"unknown key {key!r}")


def _ask_box(on_submit) -> None:                        # pragma: no cover (needs a display)
    import tkinter as tk
    root = tk.Tk()
    root.title("Lucy")
    root.attributes("-topmost", True)
    root.overrideredirect(True)
    w, h = 560, 52
    x = (root.winfo_screenwidth() - w) // 2
    y = int(root.winfo_screenheight() * 0.28)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.configure(bg="#1b1b1f")
    entry = tk.Entry(root, font=("Segoe UI", 15), bg="#26262c", fg="#f2f2f5", insertbackground="#f2f2f5", relief="flat")
    entry.pack(fill="both", expand=True, padx=8, pady=8)

    def go(_e=None):
        text = entry.get().strip()
        root.destroy()
        if text:
            on_submit(text)

    entry.bind("<Return>", go)
    entry.bind("<Escape>", lambda _e: root.destroy())
    entry.bind("<FocusOut>", lambda _e: root.after(150, lambda: root.destroy() if root.winfo_exists() else None))
    root.after(50, lambda: (root.focus_force(), entry.focus_force()))
    root.mainloop()


def _hotkey_loop(spec: str, trigger) -> None:           # pragma: no cover (Windows only)
    import ctypes
    from ctypes import wintypes
    mods, vk = parse_hotkey(spec)
    u = ctypes.windll.user32
    if not u.RegisterHotKey(None, 1, mods | 0x4000, vk):          # 0x4000 = MOD_NOREPEAT
        print(f"Could not register {spec} (another program uses it?). Set LUCY_QUICK_KEY to something else.")
        return
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312:                                # WM_HOTKEY
            trigger()


def main() -> int:                                      # pragma: no cover
    if os.name != "nt":
        print("Quick entry uses a Windows global hotkey; it only runs on Windows.")
        return 1
    spec = os.environ.get("LUCY_QUICK_KEY", "ctrl+alt+space")
    busy = threading.Lock()

    def open_box():
        if busy.acquire(blocking=False):
            try:
                _ask_box(lambda t: webbrowser.open(quick_url(t)))
            finally:
                busy.release()

    threading.Thread(target=lambda: _hotkey_loop(spec, lambda: threading.Thread(target=open_box, daemon=True).start()),
                     daemon=True).start()
    try:
        import pystray
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (64, 64), "#7c5cff")
        ImageDraw.Draw(img).ellipse((18, 18, 46, 46), fill="white")
        icon = pystray.Icon("lucy", img, "Lucy", pystray.Menu(
            pystray.MenuItem("Open Lucy", lambda: webbrowser.open(quick_url(""))),
            pystray.MenuItem("Quick entry", lambda: threading.Thread(target=open_box, daemon=True).start()),
            pystray.MenuItem("Quit", lambda icon, _item: icon.stop())))
        icon.run()
    except ImportError:
        threading.Event().wait()                                  # no tray: just keep the hotkey alive
    return 0


if __name__ == "__main__":                              # pragma: no cover
    sys.exit(main())
