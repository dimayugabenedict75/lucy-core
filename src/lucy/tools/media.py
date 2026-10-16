"""Media tools: analyze_image, send_tts, send_sticker. Moved out of api.py with the code kept as-is.
Only changes: helper names (resolve_path, _paths.SAFE_ROOT), the tool-name list comes from ctx.tool_names,
and the sticker library is built per call (it holds no state)."""
import asyncio
import base64
import json
import logging
import subprocess
import time
from pathlib import Path

import httpx

from lucy import paths as _paths
from lucy.server.llm_manager import llm
from lucy.server.stickers_tool import StickerLibrary, send_sticker as _send_sticker
from lucy.server.tts_filter import clean_for_speech
from ._common import resolve_path

logger = logging.getLogger("lucy.core.api")      # same logger name as before
_ROOT_POSIX = _paths.ROOT.as_posix()


async def _analyze_image(image_path: str, question: str = "") -> str:
    """Analyze an image using the local llama-server multimodal model."""
    import base64

    # Read the image and encode as base64 data URL
    target = Path(image_path)
    if not target.exists():
        return f"Image not found: {image_path}"

    # Determine MIME type from extension
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    mime_type = mime_map.get(target.suffix.lower(), "image/png")

    image_b64 = base64.b64encode(target.read_bytes()).decode("utf-8")
    data_url = f"data:{mime_type};base64,{image_b64}"

    # Build the prompt for image analysis
    prompt = question if question else "Describe this image in detail. Include all visible text, objects, colors, layout, and any notable details."

    # Send to llama-server multimodal endpoint
    payload = {
        "model": llm.model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 2048,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{llm.api_url}/chat/completions",
                json=payload,
            )
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    return result["choices"][0]["message"]["content"]
                return "[No content returned from image analysis]"
            else:
                return f"[Image analysis failed: status {resp.status_code} — {resp.text[:500]}]"
    except Exception as e:
        return f"[Image analysis error: {e}]"


_SCHEMA_ANALYZE_IMAGE = {
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": "Analyze an image file that was uploaded in this conversation. The image must have been sent as a file upload. Returns a detailed description of what is seen in the image, including objects, text, colors, layout, and any notable details. Use this whenever the user attaches an image and asks you to describe, explain, read, or analyze it.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": f"Path to the uploaded image file on disk (e.g. {_ROOT_POSIX}/runtime/tmp/screenshot.png)"
                },
                "question": {
                    "type": "string",
                    "description": "What to analyze or ask about in the image (e.g. 'describe this code', 'read the text', 'what colors are used')"
                }
            },
            "required": ["image_path"]
        }
    }
}

async def _h_analyze_image(args, ctx):
    image_path = args.get("image_path", "")
    question = args.get("question", "")
    target = resolve_path(image_path)
    if target is None or not target.exists():
        return f"Image not found or outside allowed path: {image_path}"
    return await _analyze_image(str(target), question)


_SCHEMA_SEND_TTS = {
    "type": "function",
    "function": {
        "name": "send_tts",
        "description": "Synthesize speech from text using the CielVox 2.6 TTS model (via the Lucy Audio server on port 8091). Returns a MEDIA: path to a WAV file that the client plays automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to synthesize. Keep under ~500 characters per call for best quality and streaming behavior."
                },
                "voice": {
                    "type": "string",
                    "description": "Voice preset to use (e.g. 'frieren'). Defaults to the model's configured default.",
                    "default": "frieren"
                }
            },
            "required": ["text"]
        }
    }
}

async def _h_send_tts(args, ctx):
    text = args.get("text", "")
    voice = args.get("voice", "frieren")
    if not text:
        return "send_tts requires 'text' parameter"
    text = clean_for_speech(text, ctx.tool_names)   # no tool calls / file paths / markdown in the voice
    if not text:
        return "send_tts: nothing speakable left after removing tool calls, file paths and markup"
    # Save the text to a temp input file for the wrapper to read
    import uuid as _uuid
    _ts = time.strftime("%Y%m%d_%H%M%S")
    input_file = _paths.HERMES_HOME / "cache" / "scratch" / f"tts_input_{_uuid.uuid4().hex[:8]}.txt"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_text(text, encoding="utf-8")
    # Call the wrapper which proxies to lucy audio server on 8091
    # Use the baked-in wrapper inside Lucy_Core/lucy_audio/ first;
    # fall back to the Hermes scripts dir for backwards compat.
    baked_wrapper = _paths.ROOT / "lucy_audio" / "build" / "windows-vulkan-release" / "bin" / "tts-wrapper-lucy-cielvox26.bat"
    hermes_wrapper = _paths.HERMES_HOME / "scripts" / "tts-wrapper-lucy-cielvox26.bat"
    wrapper = baked_wrapper if baked_wrapper.exists() else hermes_wrapper
    output_file = _paths.HERMES_HOME / "cache" / "audio" / f"tts_lucy_{_uuid.uuid4().hex[:8]}.wav"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # --- Delta stream log ---
    log_path = _paths.RUNTIME / "logs" / "deltastream_tts.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log_deltastream(status: str, msg: str = "", media_path: str = ""):
        entry = {
            "timestamp": _ts,
            "session": "current",
            "tool": "send_tts",
            "status": status,
            "voice": voice,
            "text_preview": text[:80] + ("..." if len(text) > 80 else ""),
            "message": msg,
            "media": media_path,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    _log_deltastream("started", text)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["cmd", "/c", str(wrapper), str(input_file), str(output_file)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            _log_deltastream("error", f"wrapper exit {result.returncode}: {result.stderr[:200]}")
            logger.error(f"TTS wrapper failed: {result.stderr}")
            return f"TTS failed: {result.stderr[:200]}"
        if output_file.exists() and output_file.stat().st_size > 1000:
            _log_deltastream("success", f"WAV {output_file.stat().st_size} bytes", str(output_file).replace("\\", "/"))
            # Return MEDIA: path with forward slashes for frontend compatibility
            return f"MEDIA:{str(output_file).replace(chr(92), '/')}"
        _log_deltastream("error", f"output missing/too small (size={output_file.stat().st_size if output_file.exists() else 0})")
        return f"TTS error: output file missing or too small"
    except subprocess.TimeoutExpired:
        _log_deltastream("timeout", "wrapper exceeded 120s")
        logger.error("TTS wrapper timed out after 120s")
        return f"TTS error: lucy audio server timeout (120s)"
    except Exception as e:
        _log_deltastream("error", str(e))
        logger.error(f"TTS call error: {e}")
        return f"TTS error: {e}"


_SCHEMA_SEND_STICKER = {
    "type": "function",
    "function": {
        "name": "send_sticker",
        "description": (
            "Send one of YOUR OWN stickers to the user in the chat to express an emotion or reaction. The "
            "stickers are real image files in Lucy_Core/assets/stickers/<Theme>/ (themes: Dark, Light) - you "
            "do have a sticker library. Pick the emotion that fits the moment; use it naturally and "
            "sparingly, with a short remark or none. This tool is the ONLY way to send a sticker: never "
            "write file paths or MEDIA: lines yourself. Call it as a real tool call, not as text."),
        "parameters": {
            "type": "object",
            "properties": {
                "emotion": {"type": "string", "enum": StickerLibrary(_paths.ROOT).all_emotions(), "description": "Which sticker (by emotion). Same set in both themes."},
                "theme": {"type": "string", "enum": ["Dark", "Light"], "description": "Sticker theme (default Dark)."},
                "caption": {"type": "string", "description": "Optional short caption shown with the sticker."}
            },
            "required": ["emotion"]
        }
    }
}

async def _h_send_sticker(args, ctx):
    return await asyncio.to_thread(_send_sticker, args, StickerLibrary(_paths.ROOT, _paths.SAFE_ROOT))


TOOLS = [
    {"schema": _SCHEMA_ANALYZE_IMAGE, "handler": _h_analyze_image, "tier": "read", "label": "analyze"},
    {"schema": _SCHEMA_SEND_TTS, "handler": _h_send_tts, "tier": "exec", "label": "speak"},
    {"schema": _SCHEMA_SEND_STICKER, "handler": _h_send_sticker, "tier": "read", "label": "send_sticker"},
]
