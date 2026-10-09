# Lucy Core CUA backend (Win32 API)
# Flat functions — import the module directly
from .lucy_cua import (
    get_cursor_pos,
    set_cursor_pos,
    mouse_click,
    mouse_drag,
    key_combo,
    type_text,
    minimize_all,
    show_desktop,
    restore_windows,
    press_enter,
    press_escape,
    press_return,
    win_d,
    VK_MAP,
)

__all__ = [
    "get_cursor_pos", "set_cursor_pos", "mouse_click", "mouse_drag",
    "key_combo", "type_text", "minimize_all", "show_desktop",
    "restore_windows", "press_enter", "press_escape", "press_return",
    "win_d", "VK_MAP",
]
