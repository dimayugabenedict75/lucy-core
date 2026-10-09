#!/usr/bin/env python3
"""
lucy_cua.py — Custom computer-use backend for Windows.

Uses direct Win32 APIs (ctypes → user32.dll) to control the real mouse
cursor and keyboard, bypassing the cua-driver overlay/synthetic-event
limitations.

Capabilities:
  - get_cursor_pos()       → (x, y) in screen coordinates
  - set_cursor_pos(x, y)   → move real cursor (not overlay)
  - mouse_click(x, y, button) → move + click
  - mouse_drag(x1,y1,x2,y2)   → drag gesture
  - key_combo(keys)        → press key combo (e.g. ['win','d'])
  - type_text(text)        → type a string
  - minimize_all()         → Win+D
  - show_desktop()         → Win+D
  - restore_windows()      → Win+D (toggle)
"""

import ctypes
from ctypes import wintypes
import time
import json
import sys

# ── Constants ─────────────────────────────────────────────────────
LONG     = wintypes.LONG
DWORD    = wintypes.DWORD
WORD     = wintypes.WORD
ULONG    = wintypes.ULONG

MOUSEEVENTF_LEFTDOWN   = 0x0002
MOUSEEVENTF_LEFTUP     = 0x0004
MOUSEEVENTF_RIGHTDOWN  = 0x0008
MOUSEEVENTF_RIGHTUP    = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP   = 0x0040
MOUSEEVENTF_MOVE       = 0x0001

KEYEVENTF_KEYUP        = 0x0002

# Virtual key codes
VK_LWIN     = 0x5B
VK_RWIN     = 0x5C
VK_D        = 0x44
VK_RETURN   = 0x0D
VK_ESCAPE   = 0x1B
VK_CONTROL  = 0x11
VK_MENU     = 0x12   # Alt
VK_SHIFT    = 0x10
VK_TAB      = 0x09

# ── Structure definitions ─────────────────────────────────────────
class POINT(ctypes.Structure):
    _fields_ = [('x', LONG), ('y', LONG)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ('dx', LONG),
        ('dy', LONG),
        ('mouseData', DWORD),
        ('dwFlags', DWORD),
        ('time', DWORD),
        ('dwExtraInfo', ctypes.POINTER(ULONG)),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ('wVk', WORD),
        ('wScan', WORD),
        ('dwFlags', DWORD),
        ('time', DWORD),
        ('dwExtraInfo', ctypes.POINTER(ULONG)),
    ]

class INPUT(ctypes.Structure):
    _anonymous_ = ('mi', 'ki')
    _fields_ = [
        ('type', DWORD),
        ('mi', MOUSEINPUT),
        ('ki', KEYBDINPUT),
    ]

# ── Virtual key map ──────────────────────────────────────────────
VK_MAP = {
    'win': VK_LWIN, 'rwin': VK_RWIN,
    'd': VK_D, 'return': VK_RETURN, 'enter': VK_RETURN,
    'escape': VK_ESCAPE, 'esc': VK_ESCAPE,
    'ctrl': VK_CONTROL, 'control': VK_CONTROL,
    'alt': VK_MENU, 'shift': VK_SHIFT,
    'tab': VK_TAB,
    'a': 0x41, 'b': 0x42, 'c': 0x43, 'e': 0x45,
    'f': 0x46, 'g': 0x47, 'h': 0x48, 'i': 0x49,
    'j': 0x4A, 'k': 0x4B, 'l': 0x4C, 'm': 0x4D,
    'n': 0x4E, 'o': 0x4F, 'p': 0x50, 'q': 0x51,
    'r': 0x52, 's': 0x53, 't': 0x54, 'u': 0x55,
    'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59,
    'z': 0x5A,
    '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33,
    '4': 0x34, '5': 0x35, '6': 0x36, '7': 0x37,
    '8': 0x38, '9': 0x39,
    ' ': 0x20, 'slash': 0x2F, 'backslash': 0x5C,
    'comma': 0x2C, 'period': 0x2E, 'colon': 0x3A,
    'capslock': 0x14,
}

# ── Cursor ────────────────────────────────────────────────────────
def get_cursor_pos():
    """Get current real cursor position in screen coordinates."""
    pt = POINT()
    if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return {'x': pt.x, 'y': pt.y}
    return {'error': 'GetCursorPos failed'}

def set_cursor_pos(x, y):
    """Move real cursor to (x, y) in screen coordinates."""
    ok = ctypes.windll.user32.SetCursorPos(x, y)
    return {'moved': ok, 'x': x, 'y': y}

def mouse_click(x, y, button='left'):
    """Move cursor to (x, y) and click."""
    set_cursor_pos(x, y)
    time.sleep(0.03)
    extra = ctypes.c_ulong(0)
    if button == 'left':
        down_f, up_f = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    elif button == 'right':
        down_f, up_f = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    elif button == 'middle':
        down_f, up_f = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
    else:
        return {'error': 'unknown button'}
    down = INPUT()
    down.type = 0
    down.mi = MOUSEINPUT(0, 0, 0, down_f, 0, ctypes.pointer(extra))
    up = INPUT()
    up.type = 0
    up.mi = MOUSEINPUT(0, 0, 0, up_f, 0, ctypes.pointer(extra))
    ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    return {'clicked': True, 'x': x, 'y': y, 'button': button}

def mouse_drag(x1, y1, x2, y2):
    """Drag from (x1,y1) to (x2,y2)."""
    set_cursor_pos(x1, y1)
    time.sleep(0.03)
    extra = ctypes.c_ulong(0)
    down = INPUT()
    down.type = 0
    down.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, ctypes.pointer(extra))
    ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    set_cursor_pos(x2, y2)
    up = INPUT()
    up.type = 0
    up.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, ctypes.pointer(extra))
    ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    return {'dragged': True, 'from': (x1, y1), 'to': (x2, y2)}

# ── Keyboard ──────────────────────────────────────────────────────
def key_combo(keys):
    """Press a combo of keys (e.g. ['win', 'd']).
    Sends one key at a time: down, brief delay, up."""
    total_sent = 0
    for k in keys:
        vk = VK_MAP.get(k.lower())
        if vk is None:
            return {'error': f'unknown key: {k}'}
        extra = ctypes.c_ulong(0)
        # Key down
        down = INPUT()
        down.type = 1
        down.ki = KEYBDINPUT(vk, 0, 0, 0, ctypes.pointer(extra))
        n = ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        total_sent += n
        time.sleep(0.03)
        # Key up
        up = INPUT()
        up.type = 1
        up.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))
        n = ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
        total_sent += n
        time.sleep(0.03)
    return {'pressed': True, 'keys': keys, 'events_sent': total_sent,
            'note': 'SendInput may return 0 for keyboard events even when delivered'}

def type_text(text):
    """Type a string character by character."""
    results = []
    for ch in text:
        vk = VK_MAP.get(ch.lower())
        if vk is None:
            results.append({'skipped': ch, 'reason': 'unknown key'})
            continue
        extra = ctypes.c_ulong(0)
        down = INPUT()
        down.type = 1
        down.ki = KEYBDINPUT(vk, 0, 0, 0, ctypes.pointer(extra))
        ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.02)
        up = INPUT()
        up.type = 1
        up.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))
        ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
        time.sleep(0.02)
        results.append({'typed': ch})
    return {'typed': len(results), 'results': results}

# ── Macros ────────────────────────────────────────────────────────
def minimize_all():
    """Press Win+D to minimize all windows."""
    return key_combo(['win', 'd'])

def show_desktop():
    """Same as minimize_all — Win+D toggles."""
    return minimize_all()

def restore_windows():
    """Press Win+D again to restore."""
    return minimize_all()

def press_enter():
    """Press Enter."""
    return key_combo(['enter'])

def press_escape():
    """Press Escape."""
    return key_combo(['escape'])

def press_return():
    """Same as press_enter."""
    return key_combo(['return'])

def win_d():
    """Alias for minimize_all."""
    return minimize_all()

# ── CLI ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'test'

    if action == 'test':
        print('Cursor before:', get_cursor_pos())
        print('Move to (1000,1000)...')
        print(set_cursor_pos(1000, 1000))
        print('Cursor now:', get_cursor_pos())
        print('Click at (1000,1000)...')
        print(mouse_click(1000, 1000, 'left'))
        print('Cursor after click:', get_cursor_pos())
        print('Minimize all (Win+D)...')
        print(minimize_all())
        print('Restore cursor to (1576,1294)...')
        set_cursor_pos(1576, 1294)
        print('Done!')

    elif action == 'cursor':
        print(json.dumps(get_cursor_pos()))

    elif action == 'move':
        x = int(sys.argv[2])
        y = int(sys.argv[3])
        print(json.dumps(set_cursor_pos(x, y)))

    elif action == 'click':
        x = int(sys.argv[2])
        y = int(sys.argv[3])
        btn = sys.argv[4] if len(sys.argv) > 4 else 'left'
        print(json.dumps(mouse_click(x, y, btn)))

    elif action == 'drag':
        x1 = int(sys.argv[2]); y1 = int(sys.argv[3])
        x2 = int(sys.argv[4]); y2 = int(sys.argv[5])
        print(json.dumps(mouse_drag(x1, y1, x2, y2)))

    elif action == 'hotkey':
        keys = sys.argv[2].split(',')
        print(json.dumps(key_combo(keys)))

    elif action == 'minimize':
        print(json.dumps(minimize_all()))

    elif action == 'type':
        text = sys.argv[2]
        print(json.dumps(type_text(text)))

    else:
        print(json.dumps({'error': f'unknown action: {action}'}))
