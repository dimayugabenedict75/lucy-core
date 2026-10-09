# cua-driver Windows Bugs (v0.22.2)

Diagnostic reference for when to replace cua-driver with the Windows-native backend.

## Bug 1: Named-pipe creation fails on custom sockets

**Symptom:** `cua-driver serve --socket \\.\pipe\custom` (with or without `--embedded`) fails immediately with:

```
create named pipe \\.\pipe\custom: The filename, directory name, or volume label syntax is incorrect. (os error 123)
```

**Scope:** Affects ANY `--socket` path other than the literal default `cua-driver`. The default pipe name `\\.\pipe\cua-driver` (no `--socket` flag) works fine.

**Root cause:** Bug in cua-driver-rust's named-pipe creation on Windows. Not a Python mangling issue — the command line is passed correctly to CreateProcess.

**Versions affected:** cua-driver 0.22.2 (latest as of 2026-08-30). Tested both stable and nightly channels — same result.

**Workaround:** Use only the default pipe (`cua-driver serve` with no `--socket`).

**Real fix:** Replace cua-driver with the Windows-native backend (see parent SKILL.md).

## Bug 2: move_cursor is overlay-only

**Symptom:** `computer_use(action="move_cursor", x=1000, y=1000)` returns:

```json
{"effect": "unverifiable", "route": "synthetic_events"}
```

The real OS cursor does NOT move. Only the cua-driver agent cursor overlay moves.

**Verification:** `get_cursor_position` always returns the same position regardless of `move_cursor` calls.

**Impact:** Any automation that relies on moving the cursor to specific coordinates fails silently. The model thinks it moved the cursor but it didn't.

**Workaround:** None via cua-driver. Use the Windows-native backend which calls `SetCursorPos` directly.

## Bug 3: Background keyboard input dropped

**Symptom:** `computer_use(action="key", keys="win+d")` returns:

```json
{"code": "background_unavailable", "message": "background input is dropped by this surface; retry with delivery_mode: foreground"}
```

The keyboard event is not delivered in background mode.

**Scope:** Affects all keyboard input when the target window is not foreground. Global hotkeys like Win+D require foreground delivery.

**Workaround:** Use `delivery_mode: "foreground"` — but this briefly raises the window and steals focus, which violates the background-first principle. Also, global hotkeys (Win+D) need a target PID which may not be available.

**Real fix:** Use the Windows-native backend which sends `SendInput` directly — works in background for most cases (Win+D verified working).

## Bug 4: cursor_position returns stale data

While `get_cursor_position` does return the real cursor position (unlike `move_cursor` which is overlay-only), the position doesn't update after `move_cursor` calls because those calls don't actually move the real cursor. This creates confusing state where the model thinks it moved but `get_cursor_position` shows it didn't.

## Summary: when to abandon cua-driver on Windows

Replace cua-driver with the Windows-native backend when ANY of these are true:
- You need real cursor movement (not overlay)
- You need global hotkeys (Win+D, Alt+Tab, etc.)
- You need background keyboard input
- You need custom named pipes (multi-instance, private sockets)
- `computer_use doctor` reports cua-driver issues

The Windows-native backend (`windows-native-computer-use` skill) handles all of these via direct Win32 APIs.
