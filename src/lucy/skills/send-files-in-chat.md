---
skill_id: send-files-in-chat
name: File Sending in Chat
description: Send local files as attachments through the chat by using the send_file tool after creating them
trigger: send file, screenshot, image, attachment, send to chat, show me the file, download
category: workflow
tools: [computer_use, send_file, read_local_file, list_local_files]
---

When the user asks to see a file (screenshots, documents, etc.):

1. Use `computer_use(action="screenshot")` to capture a screenshot, or use other tools to create/generate the file
2. After the file is created, call `send_file(file_path="/absolute/path/to/file", caption="brief description")` to signal the file to the client
3. The `send_file` tool returns a `MEDIA:/absolute/path/to/file caption` string. **Hermes parses the `MEDIA:` prefix** from tool output and sends the file as a Discord/Telegram attachment. Without this prefix, the file path is just text — the user sees no attachment.

Files should be saved under `data/uploads/` directory. Screenshots taken via `computer_use(action="screenshot")` are automatically saved there.

### Critical pitfall: use absolute paths

`send_file` resolves the path and must return an **absolute** path in the `MEDIA:` marker. Hermes needs the full path to locate and attach the file. Relative paths like `data/uploads/image.png` will fail silently — the tool returns "File not found" because it can't resolve relative to the sandboxed root.

Example flow:
- User: "Take a screenshot"
- Agent calls `computer_use(action="screenshot")` → saves to `/absolute/path/to/data/uploads/cua_screenshot_<timestamp>.png`
- Agent calls `send_file(file_path="/absolute/path/to/data/uploads/cua_screenshot_<timestamp>.png", caption="Current desktop view")` — this is the path returned by computer_use's stdout
- Client receives the file as an attachment, not just text

If the user asks to see an existing file on disk, use `list_local_files` first to find it, then `send_file` with the absolute path returned by `list_local_files`.
