---
skill_id: computer-use
name: Desktop Automation
description: Control the desktop via Win32 API — screenshots, mouse, keyboard
trigger: click, type, screenshot, desktop automation, press key
category: system
tools:
  - computer_use
  - read_local_file
---

# Desktop Automation (CUA)

Control the desktop using the `computer_use` tool, which wraps Win32 API calls
via the `computer_use.py` backend script.

## Actions

| Action | Parameters | Description |
|--------|------------|-------------|
| `screenshot` | x=None, y=None | Capture full screen to `data/uploads/` |
| `click` | x, y | Mouse click at screen coordinates |
| `move` | x, y | Move cursor to coordinates |
| `type` | text | Type text at cursor position |
| `key` | text | Press special key (enter, tab, esc, space, back) |

## Usage

```python
# Take a screenshot
computer_use(action="screenshot")

# Click at coordinates
computer_use(action="click", x=1920, y=1080)

# Type text
computer_use(action="type", text="Hello, world!")

# Press Enter
computer_use(action="key", text="enter")
```

## Notes

- Backend: `computer_use.py` uses `pyautogui` (mouse/keyboard) + `PIL ImageGrab` (screenshots)
- All outputs sandboxed under `C:/Users/dimay`
- 10s timeout on all operations
- Coordinate system: screen pixels (0,0 = top-left)
