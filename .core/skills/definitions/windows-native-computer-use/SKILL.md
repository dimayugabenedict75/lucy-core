---
name: windows-native-computer-use
description: "Build Win32 API backends when cua-driver fails on Windows."
version: 1.0.0
author: Lucy (lucy-agent)
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [computer-use, windows, win32, cua-driver-replacement, SendInput, SetCursorPos]
    category: desktop
    related_skills: [computer-use]
---

# Windows-Native Computer-Use Backend

When cua-driver is broken on Windows (overlay-only cursor, background input dropped, named-pipe bugs), build a direct Win32 API backend that actually controls the real cursor and keyboard.

This skill documents the technique used to replace cua-driver in lucy-agent with a ctypes-based backend.

## Identity note

In this environment, "Lucy CUA" or "our CUA" refers to `lucy_cua.py`, the standalone custom computer-use backend. That file is the developed artifact; the upstream Hermes default `cua-driver` path is a separate stack and remains broken on this Windows setup.

## When to use this

Use this skill when:
- The user refers to "Lucy CUA", "our CUA", or `lucy_cua.py`
- `computer_use` with cua-driver backend reports `effect: unverifiable` for cursor movement (overlay only, not real cursor)
- `computer_use` with cua-driver backend returns `code: background_unavailable` for keyboard input
- cua-driver named-pipe creation fails with "os error 123" on Windows (only default pipe name works)
- You need real cursor movement and global hotkeys (Win+D) on Windows
- You need a verified working desktop-control path on this machine right now

## The core approach

Use Python `ctypes` to call Win32 APIs directly:

| Action | Win32 API | Notes |
|---|---|---|
| Move cursor | `SetCursorPos(x, y)` | Moves REAL cursor, not overlay |
| Get cursor pos | `GetCursorPos()` | Returns real position |
| Mouse click | `SendInput` with MOUSEINPUT | Use MOUSEEVENTF_LEFTDOWN/UP |
| Mouse drag | `SendInput` down + SetCursorPos + up | |
| Keyboard | `SendInput` with KEYBDINPUT | VK codes via hardcoded map |
| Key combo | Send each key down+up sequentially | win, then d, etc. |
| Screenshot | GDI BitBlt → PIL Image | Use desktop DC, not window DC |

## Key ctypes setup

```python
import ctypes
from ctypes import wintypes

LONG = wintypes.LONG
DWORD = wintypes.DWORD
WORD = wintypes.WORD
ULONG = wintypes.ULONG

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004
KEYEVENTF_KEYUP      = 0x0002
VK_LWIN = 0x5B
VK_D = 0x44

class POINT(ctypes.Structure):
    _fields_ = [('x', LONG), ('y', LONG)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', LONG), ('dy', LONG), ('mouseData', DWORD),
                ('dwFlags', DWORD), ('time', DWORD), ('dwExtraInfo', ctypes.POINTER(ULONG))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', WORD), ('wScan', WORD), ('dwFlags', DWORD),
                ('time', DWORD), ('dwExtraInfo', ctypes.POINTER(ULONG))]

class INPUT(ctypes.Structure):
    _anonymous_ = ('mi', 'ki')
    _fields_ = [('type', DWORD), ('mi', MOUSEINPUT), ('ki', KEYBDINPUT)]
```

## Critical gotchas discovered

### 1. SendInput returns 0 for keyboard but still works

`SendInput` for keyboard events may return 0 (events_sent: 0) even when the keys ARE delivered. Don't trust the return count — verify by checking cursor position after Win+D, or test with a visible effect.

**Do not** report `pressed: False` based on `SendInput` return value alone. The keys are often delivered even when the return is 0.

### 2. PIL 12.3.0 removed Image.grab()

PIL 12.x removed `Image.grab()` (was deprecated). Use GDI BitBlt instead:

```python
# Full desktop screenshot
hdcSrc = user32.GetDC(0)  # desktop DC
hdcMem = gdi32.CreateCompatibleDC(hdcSrc)
hbm = gdi32.CreateCompatibleBitmap(hdcSrc, width, height)
gdi32.SelectObject(hdcMem, hbm)
gdi32.BitBlt(hdcMem, 0, 0, width, height, hdcSrc, 0, 0, SRCCOPY)
# GetDIBits → PIL.frombytes('RGB', (w, h), buf.raw)
```

Use `buf.raw` (not `buf`) and skip the raw decoder — `Image.frombytes('RGB', (w, h), buf.raw)` works. The `'raw', 'BGRX'` decoder is unreliable on Windows.

### 3. Windows foreground lock blocks SetForegroundWindow

Background processes cannot steal focus. Use `AttachThreadInput` to bypass:

```python
import win32process, win32gui, win32con

fg_hwnd = win32gui.GetForegroundWindow()
fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
our_thread = win32process.GetWindowThreadProcessId(hwnd)[0]

if fg_thread and our_thread and fg_thread != our_thread:
    win32process.AttachThreadInput(fg_thread, our_thread, True)
    win32gui.SetForegroundWindow(hwnd)
    win32process.AttachThreadInput(fg_thread, our_thread, False)
else:
    win32gui.SetForegroundWindow(hwnd)
win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
```

This is needed for `focus_app(raise_window=True)` and `bring_to_front`.

### 4. cua-driver named-pipe bug on Windows

cua-driver 0.22.2 cannot create ANY named pipe except the default `\\.\pipe\cua-driver`. Any `--socket` path other than the literal default fails with "os error 123". This affects both `--embedded` and non-embedded modes.

Workaround: use the default pipe only, or replace cua-driver entirely (this skill).

### 5. cua-driver move_cursor is overlay-only

cua-driver's `move_cursor` moves an agent cursor overlay, NOT the real OS cursor. It returns `effect: unverifiable`. For real cursor movement, you need a different backend (this skill).

## Virtual key code map

For keyboard input, you need VK codes. Common ones:

```python
VK_MAP = {
    'win': 0x5B, 'rwin': 0x5C,
    'd': 0x44, 'return': 0x0D, 'enter': 0x0D,
    'escape': 0x1B, 'esc': 0x1B,
    'ctrl': 0x11, 'alt': 0x12, 'shift': 0x10,
    'tab': 0x09,
    # Letters a-z: 0x41-0x5A
    # Numbers 0-9: 0x30-0x39
    # Space: 0x20
}
```

For letters and numbers outside this map, use `ord('a')` → 0x61, then convert to VK via `MapVirtualKey` or just use the ASCII code directly (SendInput accepts wVk as the virtual key code).

## Full replacement in lucy-agent

To replace cua-driver in lucy-agent:

1. **Rewrite `tools/computer_use/cua_backend.py`** — replace the MCP/asyncio/cua-driver session machinery with direct Win32 calls. Keep the same `CuaDriverBackend` class interface (constructor, `start()`, `stop()`, `is_available()`, all action methods).

2. **Keep the class name `CuaDriverBackend`** — tool.py imports it by that name. The implementation changes, the interface stays.

3. **Stub out unsupported features** — typed browser, recording, agent cursor overlay, accessibility tree. Return `{"ok": False, "message": "...not supported..."}` for these.

4. **Config stays `computer_use.backend: cua`** — since cua_backend.py now contains the Windows-native implementation, the config value still works. The `windows` backend name also works (added as alias in tool.py).

## What this backend can and cannot do

### Can do (verified working)
- Real cursor movement (SetCursorPos)
- Mouse click at coordinates (SendInput)
- Mouse drag
- Mouse scroll
- Keyboard single key
- Keyboard hotkey (Win+D, Ctrl+C, Alt+Tab, etc.)
- Type text character-by-character
- Window screenshot (GDI BitBlt → PIL)
- Full desktop screenshot
- Window enumeration (EnumWindows via pywin32)
- App list (derived from windows)
- Focus app (SetForegroundWindow + AttachThreadInput)
- Bring to front (same approach)
- Launch app (subprocess.Popen + FindWindow)
- Kill app (psutil.Process.kill + taskkill fallback)
- Get cursor position
- Get screen size (GetSystemMetrics)
- Zoom on window region

### Cannot do (stubbed)
- Typed browser (CDP/browser automation)
- Agent cursor overlay (no overlay in Win32-native approach)
- Recording/replay
- Config persistence
- Accessibility tree (returns window list instead)
- Browser page tools

## Files produced this session

- `C:\Users\dimay\AppData\Local\hermes\lucy_cua.py` — standalone version (Hermes home, 297 lines)
- `C:\Users\dimay\Lucy\projects\lucy-agent\tools/computer_use\cua_backend.py` — lucy-agent replacement (1309 lines, was 3955)
- `C:\Users\dimay\Lucy\projects\lucy-agent\tools/computer_use\cua_backend.py.bak` — backup of original cua_backend.py (179KB)
- `C:\Users\dimay\Lucy\projects\lucy-agent\tools/computer_use\lucy_cua_backend.py` — lucy-agent copy of standalone backend (285 lines)

## Interface contract the replacement must preserve

The new `CuaDriverBackend` class must implement the same public surface the original had, because `tools/computer_use/tool.py` imports and instantiates it directly. Required methods from the original:

`start()`, `stop()`, `is_available()`, `capture()`, `move_cursor()`, `click()`, `drag()`, `scroll()`, `type_text()`, `key()`, `hotkey()`, `set_value()`, `list_windows()`, `list_apps()`, `focus_app()`, `launch_app()`, `kill_app()`, `bring_to_front()`, `get_cursor_position()`, `get_screen_size()`, `zoom()`, `typed_browser_state()`, `typed_browser_prepare()`, `typed_browser_action()`, `set_agent_cursor_enabled()`, `set_agent_cursor_motion()`, `set_agent_cursor_style()`, `get_agent_cursor_state()`, `start_recording()`, `stop_recording()`, `get_recording_state()`, `replay_trajectory()`, `install_ffmpeg()`, `get_config()`, `set_config()`, `get_accessibility_tree()`, `page()`.

Unsupported features should return `{"ok": False, "message": "...not supported..."}` rather than raising, to avoid breaking callers that catch errors.

## tool.py backend alias

After replacement, add a `windows` backend alias in `tools/computer_use/tool.py` so both `cua` and `windows` config values map to the same `CuaDriverBackend`:

```python
elif backend_name in ("cua", "cua-driver", "windows"):
    from tools.computer_use.cua_backend import CuaDriverBackend
    return CuaDriverBackend(permission_mode=permission_mode, ...)
```

This lets existing `computer_use.backend: cua` config keep working without changes, while also accepting `windows` as an explicit name.

## Standalone lucy_cua.py restore pattern

The standalone `lucy_cua.py` in Hermes home is a separate artifact from the lucy-agent module. It can be deleted and restored independently. If restoring from scratch, the file is 297 lines and covers: cursor movement, mouse click/drag, keyboard hotkey, type_text, minimize-all via Win+D, and screen capture via GDI. Test with:

```bash
python "C:\Users\dimay\AppData\Local\hermes\lucy_cua.py" test
```

Expected output includes cursor coordinates moving from current position → target → click → Win+D minimize → restore.

## Verified end-to-end results (Windows-native backend)

- `capture`: returns `width`, `height`, `png_b64`, `app`, `window_title` — working on full desktop and per-window
- `move_cursor`: `True Moved cursor to (x, y)` — real OS cursor moves
- `click`: `True Clicked left at (x, y)` — real mouse click delivered
- `type_text`: `True Typed N characters` — character-by-character via SendInput
- `hotkey (win+d)`: `True Pressed 'win+d'` — minimize-all works, cursor lands on desktop
- `list_windows`: returns list of `{pid, name, title, window_id}` entries
- `focus_app (Discord)`: `True Targeted ... with raise.` — AttachThreadInput bypasses foreground lock
- `list_apps`: derived from window enumeration
- `launch_app (calc)`: returns `{pid, name, bundle_id, windows}`
- `kill_app`: `True Terminated process <pid>`
- `bring_to_front`: works via AttachThreadInput + SetForegroundWindow + ShowWindow(SW_RESTORE)
- `get_cursor_position`: returns real `(x, y)`
- `get_screen_size`: returns `{"width": N, "height": M}` from GetSystemMetrics
- `zoom`: works on window region

## See also

- `references/cua-driver-windows-bugs.md` — detailed diagnostic reference for each cua-driver bug and when to abandon it

## Lucy Core Integration

The standalone `lucy_cua.py` backend (288 lines) has been integrated into Lucy Core as a dedicated CUA module.

**Location:** `C:/Users/dimay/Lucy/Lucy_Core/src/lucy/cua/`

**Files:**
- `cua/__init__.py` — exports all public functions
- `cua/lucy_cua.py` — the Win32 API backend (copy of the standalone artifact)

**Import path:**
```python
from lucy.cua import get_cursor_pos, set_cursor_pos, mouse_click, key_combo, type_text
```

### API integration pattern

When wiring lucy_cua into a FastAPI server (e.g. Lucy Core's `api.py`), expose it as a **POST /api/cua** endpoint accepting form fields. The tool execution dispatch follows a 3-tool structure:

| Tool name | Actions | Required args |
|---|---|---|
| `cua_cursor` | `get`, `move` | `get` → none; `move` → x, y |
| `cua_mouse` | `click`, `move`, `drag` | click → x, y, button; drag → x1,y1,x2,y2 |
| `cua_keyboard` | `hotkey`, `type`, `minimize`, `enter`, `escape` | hotkey → keys[]; type → text |

**Tool definitions** must be registered in the server's `TOOLS` list so the model can see and call them autonomously. **Activity labels** should map all three to `"control"` prefix for the frontend.

**Important:** the existing server process does NOT auto-reload unless started with `--reload`. After adding new tools/endpoints, either restart the server or ensure uvicorn was launched with `--reload`.
