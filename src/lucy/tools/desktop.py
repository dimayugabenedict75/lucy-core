"""Desktop-control tools (mouse, keyboard, cursor). Moved out of shell.py with the code kept as-is;
the three tools share one dispatcher that calls the Win32 backend in lucy.cua.lucy_cua."""
import json
import logging

logger = logging.getLogger("lucy.core.api")      # same logger name as before


def _run_cua(tool_name, args):
    # Dispatch CUA tools to the lucy_cua module (Win32 API backend)
    from lucy.cua.lucy_cua import (
        get_cursor_pos, set_cursor_pos, mouse_click,
        mouse_drag, key_combo, type_text, minimize_all,
    )
    try:
        if tool_name == "cua_cursor":
            action = args.get("action", "get")
            if action == "get":
                return json.dumps(get_cursor_pos())
            elif action == "move":
                x, y = args["x"], args["y"]
                return json.dumps(set_cursor_pos(x, y))
            return json.dumps({"error": f"Unknown cursor action: {action}"})

        elif tool_name == "cua_mouse":
            action = args.get("action", "click")
            if action == "click":
                x, y = args["x"], args["y"]
                button = args.get("button", "left")
                return json.dumps(mouse_click(x, y, button))
            elif action == "move":
                x, y = args["x"], args["y"]
                return json.dumps(set_cursor_pos(x, y))
            elif action == "drag":
                x1, y1, x2, y2 = args["x1"], args["y1"], args["x2"], args["y2"]
                return json.dumps(mouse_drag(x1, y1, x2, y2))
            return json.dumps({"error": f"Unknown mouse action: {action}"})

        elif tool_name == "cua_keyboard":
            action = args.get("action", "press")
            if action == "hotkey":
                keys = args.get("keys", [])
                return json.dumps(key_combo(keys))
            elif action == "type":
                text_content = args.get("text", "")
                return json.dumps(type_text(text_content))
            elif action in ("minimize", "enter", "escape"):
                key_map = {"minimize": ["win", "d"], "enter": ["enter"], "escape": ["escape"]}
                return json.dumps(key_combo(key_map[action]))
            return json.dumps({"error": f"Unknown keyboard action: {action}"})

    except Exception as e:
        logger.error(f"CUA tool error ({tool_name}): {e}")
        return json.dumps({"error": str(e)})


_SCHEMA_CUA_CURSOR = {
    "type": "function",
    "function": {
        "name": "cua_cursor",
        "description": "Get or set the real OS cursor position via Win32 API. Pass action 'get' to read current position, or 'move' with x/y to move the cursor. Returns {'x': X, 'y': Y}.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "move"],
                    "description": "'get' returns current cursor position; 'move' requires x and y"
                },
                "x": {
                    "type": "integer",
                    "description": "Target X coordinate (required for action='move')"
                },
                "y": {
                    "type": "integer",
                    "description": "Target Y coordinate (required for action='move')"
                }
            },
            "required": ["action"]
        }
    }
}

def _h_cua_cursor(args, ctx):
    return _run_cua("cua_cursor", args)


_SCHEMA_CUA_MOUSE = {
    "type": "function",
    "function": {
        "name": "cua_mouse",
        "description": "Control mouse: click, drag, or move. action='click' requires x,y and optional button ('left'/'right'/'middle'). action='drag' requires x1,y1,x2,y2. action='move' requires x,y.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "drag", "move"],
                    "description": "The mouse action to perform"
                },
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "x1": {"type": "integer", "description": "Drag start X (for drag action)"},
                "y1": {"type": "integer", "description": "Drag start Y (for drag action)"},
                "x2": {"type": "integer", "description": "Drag end X (for drag action)"},
                "y2": {"type": "integer", "description": "Drag end Y (for drag action)"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left"
                }
            },
            "required": ["action"]
        }
    }
}

def _h_cua_mouse(args, ctx):
    return _run_cua("cua_mouse", args)


_SCHEMA_CUA_KEYBOARD = {
    "type": "function",
    "function": {
        "name": "cua_keyboard",
        "description": "Send keyboard input. action='hotkey' requires keys list (e.g. ['win','d']). action='type' requires text string. action='press' requires a single key name.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["hotkey", "type", "press", "minimize", "enter", "escape"],
                    "description": "The keyboard action to perform"
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key combo components (for hotkey action, e.g. ['win','d'])"
                },
                "text": {
                    "type": "string",
                    "description": "Text to type (for type action)"
                }
            },
            "required": ["action"]
        }
    }
}

def _h_cua_keyboard(args, ctx):
    return _run_cua("cua_keyboard", args)

TOOLS = [
    {"schema": _SCHEMA_CUA_CURSOR, "handler": _h_cua_cursor, "tier": "system", "label": "control"},
    {"schema": _SCHEMA_CUA_MOUSE, "handler": _h_cua_mouse, "tier": "system", "label": "control"},
    {"schema": _SCHEMA_CUA_KEYBOARD, "handler": _h_cua_keyboard, "tier": "system", "label": "control"},
]
