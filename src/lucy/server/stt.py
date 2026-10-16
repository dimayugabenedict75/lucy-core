"""
Speech-to-text for the chat UI's microphone button.

The browser records your voice, encodes it as a 16 kHz mono WAV and POSTs it to POST /api/stt.
This module turns that recording into text and returns it; the chat UI then sends the text as your message.

HOW THE AUDIO BECOMES TEXT (LUCY_STT_ENGINE)
|-----------------------------------------------
  "llm"   (default)  The recording goes to the SAME llama-server that powers the chat, as an audio input with
                     a "transcribe this" instruction. Lucy 12B / 4B are loaded with an audio-capable mmproj
                     (llama-server logs "init_audio"), so no extra speech model or server is needed.
  "audio"            The Lucy Audio Server (port 8091) transcribes it with a dedicated ASR model
                     (POST /v1/audio/transcriptions). Needs an "asr" model in lucy_audio/server.json.

Other overrides (environment variables):
    LUCY_STT_PROMPT        the instruction given to the LLM (see DEFAULT_PROMPT)
    LUCY_AUDIO_ASR_MODEL   pin the audio-server model id (engine "audio"; default: first model whose task is asr)
    LUCY_AUDIO_BASE_URL    default http://127.0.0.1:8091

WIRING (already done in the api.py that came with this file)
|--------------------------------------------------------------
    from lucy.server.stt import router as stt_router
    app.include_router(stt_router, dependencies=[Depends(verify_api_key)])
"""

import base64
import logging
import os
import re
from typing import Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

logger = logging.getLogger("lucy.core.stt")

router = APIRouter(prefix="/api/stt", tags=["stt"])

STT_ENGINE = os.environ.get("LUCY_STT_ENGINE", "llm").strip().lower()

# Wording follows Google's recommended speech-recognition prompt for Gemma-family audio models.
DEFAULT_PROMPT = (
    "Transcribe the following speech segment in its original language. "
    "Only output the transcription, with no newlines and no commentary. "
    "When transcribing numbers, write the digits. "
    "If there is no speech, output nothing."
)
STT_PROMPT = os.environ.get("LUCY_STT_PROMPT", "").strip() or DEFAULT_PROMPT

LUCY_AUDIO_BASE_URL = os.environ.get("LUCY_AUDIO_BASE_URL", "http://127.0.0.1:8091").rstrip("/")
AUDIO_STT_URL = f"{LUCY_AUDIO_BASE_URL}/v1/audio/transcriptions"
AUDIO_MODELS_URL = f"{LUCY_AUDIO_BASE_URL}/v1/models"
ASR_MODEL_OVERRIDE = os.environ.get("LUCY_AUDIO_ASR_MODEL", "").strip()
_ASR_WORDS = ("asr", "stt", "transcri", "speech_to_text", "speech-to-text", "recogni")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
LLM_MAX_TOKENS = 512     # plenty for a spoken prompt


# ---------------------------------------------------------------- helpers
def _clean_transcript(text: str) -> str:
    """The LLM sometimes wraps the transcript in quotes or a label, or says 'no speech'. Tidy that up."""
    t = " ".join((text or "").split())
    t = re.sub(r"^(?:here(?:'s| is) the transcri(?:pt|ption)|transcri(?:pt|ption))\s*[:\-]\s*", "", t, flags=re.I)
    if len(t) >= 2 and t[0] in "\"'\u201c\u2018" and t[-1] in "\"'\u201d\u2019":
        t = t[1:-1].strip()
    if re.fullmatch(r"[\[\(<*_ ]*(?:no speech|silence|inaudible|no audio|nothing)[\]\)>*_. ]*", t, flags=re.I):
        return ""
    return t


def _error_text(r: httpx.Response) -> str:
    """Best-effort readable message from an error reply (llama-server and the audio server both send JSON)."""
    try:
        j = r.json()
        err = j.get("error", j.get("detail"))
        if isinstance(err, dict):
            err = err.get("message")
        if err:
            return str(err)[:300]
    except Exception:
        pass
    return (r.text or f"HTTP {r.status_code}")[:300]


# ---------------------------------------------------------------- engine: the chat LLM
async def _transcribe_with_llm(wav: bytes) -> str:
    from lucy.server.llm_manager import llm   # imported here: llm_manager is heavy and only this engine needs it

    payload = {
        "model": llm.model_name,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"}},
                {"type": "text", "text": STT_PROMPT},
            ],
        }],
        "temperature": 0,
        "max_tokens": LLM_MAX_TOKENS,
        "stream": False,
        **llm.request_extras(),
    }
    try:
        # Generous read timeout: the request waits its turn if the model is busy answering something else.
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=3.0)) as client:
            r = await client.post(f"{llm.api_url}/chat/completions", json=payload)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="The language model is not running.")
    except httpx.RequestError as e:
        logger.warning("STT (llm) request failed: %r", e)
        raise HTTPException(status_code=502, detail="Could not reach the language model.")

    if r.status_code != 200:
        msg = _error_text(r)
        logger.warning("STT (llm) failed (%s): %s", r.status_code, msg)
        if "audio" in msg.lower() or "mmproj" in msg.lower() or "multimodal" in msg.lower():
            msg += " (This model needs an audio-capable mmproj to hear speech.)"
        raise HTTPException(status_code=502, detail=f"Transcription failed: {msg}")
    try:
        content = r.json()["choices"][0]["message"].get("content") or ""
    except Exception:
        raise HTTPException(status_code=502, detail="The language model sent an unreadable reply.")
    return _clean_transcript(content if isinstance(content, str) else "")


# ---------------------------------------------------------------- engine: Lucy Audio Server (dedicated ASR model)
async def _find_asr_model(client: httpx.AsyncClient) -> Optional[str]:
    if ASR_MODEL_OVERRIDE:
        return ASR_MODEL_OVERRIDE
    r = await client.get(AUDIO_MODELS_URL, timeout=5.0)
    r.raise_for_status()
    for m in r.json().get("data", []):
        if any(w in f"{m.get('task', '')} {m.get('family', '')}".lower() for w in _ASR_WORDS):
            return m.get("id")
    return None


async def _transcribe_with_audio_server(wav: bytes, language: Optional[str]) -> str:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=3.0)) as client:
            model = await _find_asr_model(client)
            if not model:
                raise HTTPException(status_code=503, detail=(
                    "No speech-to-text model is set up on the Lucy Audio server. "
                    "Add an \"asr\" model to lucy_audio/server.json and restart Voice."))
            form = {"model": model, "stream": "false"}
            if language:
                form["language"] = language
            r = await client.post(AUDIO_STT_URL, data=form, files={"file": ("voice.wav", wav, "audio/wav")})
    except HTTPException:
        raise
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="The Lucy Audio server is not running. Turn on Voice first.")
    except httpx.RequestError as e:
        logger.warning("STT (audio server) request failed: %r", e)
        raise HTTPException(status_code=502, detail="Could not reach the Lucy Audio server.")
    if r.status_code != 200:
        msg = _error_text(r)
        logger.warning("STT (audio server) failed (%s): %s", r.status_code, msg)
        raise HTTPException(status_code=502, detail=f"Transcription failed: {msg}")
    try:
        return (r.json().get("text") or "").strip()
    except Exception:
        raise HTTPException(status_code=502, detail="The audio server sent an unreadable reply.")


# ---------------------------------------------------------------- routes
@router.get("/status")
async def stt_status():
    """Which engine transcribes voice messages (handy when debugging)."""
    return {"engine": STT_ENGINE if STT_ENGINE in ("llm", "audio") else "llm"}


@router.post("")
async def transcribe(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
):
    """Turn a WAV recording into text. Returns {"text": "...", "engine": "llm" | "audio"}."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The recording was empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The recording is too long.")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise HTTPException(status_code=415, detail="Only WAV audio is supported.")

    engine = "audio" if STT_ENGINE == "audio" else "llm"
    text = await (_transcribe_with_audio_server(data, language) if engine == "audio" else _transcribe_with_llm(data))
    logger.info("STT ok via %s: %d bytes of audio -> %d characters", engine, len(data), len(text))
    return {"text": text, "engine": engine}
