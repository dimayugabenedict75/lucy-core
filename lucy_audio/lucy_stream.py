#!/usr/bin/env python3
"""lucy_stream - zero-dependency client for lucy_audio delta streams.

Covers every streaming route the server has:
  speak()            POST /v1/audio/speech                  (SSE speech.audio.delta)
  speak_raw()        POST /v1/audio/speech                  (stream_format=audio, raw PCM)
  transcribe_file()  POST /v1/audio/transcriptions          (multipart, stream=true)
  transcribe_live()  POST /v1/audio/transcriptions/live     (full-duplex chunked PCM in, SSE out)

CLI:
  lucy_stream.py say "hello there" --model cielvox26 --rate 24000
  lucy_stream.py file clip.wav --model nemotron-stream
  lucy_stream.py listen --model voxtral-realtime
"""

from __future__ import annotations

import base64
import http.client
import json
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import IO, Iterable, Iterator
from urllib.parse import urlencode, urlsplit


class StreamError(RuntimeError):
    """Server sent an error event, or the stream ended without [DONE]."""


# -- Audio post-processing ----------------------------------------------------

def amplify(pcm: bytes, factor: float = 3.0) -> bytes:
    """Amplify s16LE mono PCM by a fixed factor, clipping at int16 bounds.

    Lucy_Audio's raw output amplitude can be ~20-30% of full scale; a 2-5x
    gain brings conversational speech to a comfortable listening level.
    """
    import struct
    out = bytearray()
    for i in range(0, len(pcm), 2):
        sample = struct.unpack('<h', pcm[i:i+2])[0]
        sample = int(sample * factor)
        sample = max(-32768, min(32767, sample))  # clip
        out += struct.pack('<h', sample)
    return bytes(out)


# -- SSE ----------------------------------------------------------------------

def iter_sse(resp: IO[bytes]) -> Iterator[dict]:
    """Spec-correct SSE reader: multi-line data, comments, CRLF, [DONE] sentinel.

    Raises StreamError on {"type":"error"} and on EOF before [DONE] - the server
    emits an error event and then just closes, so a silent EOF means truncation.
    """
    data: list[str] = []
    while True:
        raw = resp.readline()
        if not raw:
            raise StreamError("stream closed before [DONE] (truncated)")
        line = raw.decode("utf-8").rstrip("\r\n")
        if line:
            if line.startswith(":"):
                continue                    # comment / keep-alive
            name, _, value = line.partition(":")
            if name == "data":
                data.append(value[1:] if value.startswith(" ") else value)
            continue                        # ignore event:/id:/retry:
        if not data:
            continue
        payload, data = "\n".join(data), []
        if payload == "[DONE]":
            return
        event = json.loads(payload)
        if event.get("type") == "error":
            raise StreamError(event.get("error", {}).get("message", "unknown server error"))
        yield event


# -- HTTP plumbing -----------------------------------------------------------

class Client:
    def __init__(self, base_url: str = "http://127.0.0.1:8091", timeout: float = 120.0):
        u = urlsplit(base_url)
        self.https = u.scheme == "https"
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.https else 80)
        self.timeout = timeout
                                    # per-read idle timeout, not total

    def _conn(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)

    @staticmethod
    def _check(resp: http.client.HTTPResponse) -> None:
        if resp.status != 200:
            body = resp.read().decode("utf-8", "replace")
            try:
                body = json.loads(body)["error"]["message"]
            except Exception:
                pass
            raise StreamError(f"HTTP {resp.status}: {body}")

    def _post(self, path: str, body: bytes, ctype: str, accept: str) -> tuple:
        conn = self._conn()
        conn.request("POST", path, body=body,
                     headers={"Content-Type": ctype, "Accept": accept})
        resp = conn.getresponse()
        self._check(resp)
        return conn, resp

# -- TTS ---------------------------------------------------------------------

    def speak(self, text: str, model: str, **extra) -> Iterator[bytes]:
        """Yield s16LE PCM chunks as they're generated (SSE mode)."""
        body = json.dumps({"model": model, "input": text, "response_format": "pcm",
                           "stream_format": "sse", **extra}).encode()
        conn, resp = self._post("/v1/audio/speech", body, "application/json",
                                "text/event-stream")
        try:
            for ev in iter_sse(resp):
                if ev.get("type") == "speech.audio.delta":
                    yield base64.b64decode(ev["audio"])
                elif ev.get("type") == "speech.audio.done":
                    self.last_timing = ev.get("timing")
        finally:
            conn.close()

    def speak_raw(self, text: str, model: str, chunk: int = 4096, **extra) -> Iterator[bytes]:
        """Raw PCM mode: ~33% less bandwidth than base64 SSE, but no error events
        (a failure just ends the stream early). Keeps output sample-aligned."""
        body = json.dumps({"model": model, "input": text, "response_format": "pcm",
                           "stream_format": "audio", **extra}).encode()
        conn, resp = self._post("/v1/audio/speech", body, "application/json",
                                "application/octet-stream")
        carry = b""
        try:
            while buf := resp.read1(chunk):
                buf, carry = carry + buf, b""
                if len(buf) % 2:
                    # HTTP chunks can split a sample
                    buf, carry = buf[:-1], buf[-1:]
                yield buf
        finally:
            conn.close()

# -- ASR ---------------------------------------------------------------------

    def transcribe_file(self, wav_path: str, model: str,
                        language: str | None = None) -> Iterator[dict]:
        """Upload a file, stream decode events back."""
        boundary = uuid.uuid4().hex
        parts = [("model", model), ("stream", "true")]
        if language:
            parts.append(("language", language))
        out = bytearray()
        for k, v in parts:
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n"
                    f"\r\n{v}\r\n").encode()
        with open(wav_path, "rb") as f:
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                    f"filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
            out += f.read() + b"\r\n"
        out += f"--{boundary}--\r\n".encode()
        conn, resp = self._post("/v1/audio/transcriptions", bytes(out),
                                f"multipart/form-data; boundary={boundary}",
                                "text/event-stream")
        try:
            yield from iter_sse(resp)
        finally:
            conn.close()

    def transcribe_live(self, pcm: Iterable[bytes], model: str, sample_rate: int = 16000,
                        channels: int = 1, sample_format: str = "s16le",
                        language: str | None = None) -> Iterator[dict]:
        """Full duplex: a thread pushes chunked PCM while we read SSE on the same socket."""

        # Deltas can show up while you're still talking (model permitting).

        q = {"model": model, "sample_rate": sample_rate, "channels": channels,
             "sample_format": sample_format}
        if language:
            q["language"] = language
        conn = self._conn()
        conn.putrequest("POST", "/v1/audio/transcriptions/live?" + urlencode(q),
                        skip_accept_encoding=True)
        conn.putheader("Transfer-Encoding", "chunked")  # no Expect: 100-continue
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Accept", "text/event-stream")
        conn.endheaders()

        send_err: list[BaseException] = []
        stop = threading.Event()

        def pump() -> None:
            try:
                for chunk in pcm:
                    if stop.is_set():
                        return
                    if chunk:
                        conn.send(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                conn.send(b"0\r\n\r\n")  # terminating chunk - "speaker done"
            except BaseException as e:   # noqa: BLE001 - surfaced below
                send_err.append(e)

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            resp = conn.getresponse()   # headers arrive before the body finishes
            self._check(resp)
            yield from iter_sse(resp)
        except StreamError:
            if send_err:
                raise StreamError(f"upload failed: {send_err[0]}") from send_err[0]
            raise
        finally:
            stop.set()
            conn.close()


# -- Transcript assembly -----------------------------------------------------

@dataclass
class Transcript:
    """Appends deltas; swaps in the server's authoritative text on done.

    `drifted` is True when the concatenated deltas didn't match the final text -
    happens when the decoder revises already-published words (see review notes).
    """
    text: str = ""
    final: bool = False
    drifted: bool = False
    timing: dict = field(default_factory=dict)

    def feed(self, ev: dict) -> str | None:
        kind = ev.get("type")
        if kind == "transcript.text.delta":
            if "offset" in ev:            # byte offset from a patched server
                self.text = self.text.encode()[:ev["offset"]].decode("utf-8", "ignore")
            self.text += ev["delta"]
            return ev["delta"]
        if kind == "transcript.text.done":
            self.drifted = ev["text"] != self.text
            self.text, self.final = ev["text"], True
            self.timing = ev.get("timing") or {}
            return None


# -- Local audio I/O (argv only, no shell) ----------------------------------

def player(rate: int, channels: int = 1) -> subprocess.Popen:
    for argv in ([ "pw-cat", "--playback", "--format", "s16", "--rate", str(rate),
                   "--channels", str(channels), "-" ],
                 [ "aplay", "-q", "-f", "S16_LE", "-r", str(rate), "-c", str(channels) ],
                 [ "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "s16le",
                   "-ar", str(rate), "-ch_layout", "mono" if channels == 1 else "stereo", "-" ]):
        if shutil.which(argv[0]):
            return subprocess.Popen(argv, stdin=subprocess.PIPE)
    raise RuntimeError("need pw-cat, aplay or ffplay")


def mic(rate: int = 16000, chunk_ms: int = 100) -> Iterator[bytes]:
    for argv in ([ "pw-record", "--format", "s16", "--rate", str(rate), "--channels", "1", "-" ],
                 [ "arecord", "-q", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-t", "raw" ]):
        if shutil.which(argv[0]):
            break
    else:
        raise RuntimeError("need pw-record or arecord")
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE)
    size = rate * 2 * chunk_ms // 1000
    try:
        while chunk := proc.stdout.read(size):
            yield chunk
    finally:
        proc.terminate()


# -- CLI ---------------------------------------------------------------------

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8091")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("say"); s.add_argument("text"); s.add_argument("--model", required=True)
    s.add_argument("--rate", type=int, default=24000, help="model output rate (server doesn't send it)")
    s.add_argument("--raw", action="store_true", help="raw PCM transport instead of SSE")
    s.add_argument("-o", "--out", help="write .pcm instead of playing")
    f = sub.add_parser("file"); f.add_argument("wav"); f.add_argument("--model", required=True)
    f.add_argument("--language")
    li = sub.add_parser("listen"); li.add_argument("--model", required=True)
    li.add_argument("--rate", type=int, default=16000); li.add_argument("--language")
    a = ap.parse_args()
    c = Client(a.url)

    try:
        if a.cmd == "say":
            gen = (c.speak_raw if a.raw else c.speak)(a.text, a.model)
            sink = open(a.out, "wb") if a.out else player(a.rate).stdin
            try:
                for pcm in gen:
                    sink.write(pcm); sink.flush()
            finally:
                sink.close()
        else:
            events = (c.transcribe_file(a.wav, a.model, a.language) if a.cmd == "file"
                      else c.transcribe_live(mic(a.rate), a.model, a.rate, language=a.language))
            tr = Transcript()
            for ev in events:
                if (d := tr.feed(ev)) is not None:
                    print(d, end="", flush=True)
            print()
            if tr.drifted:
                print(f"[corrected] {tr.text}")
    except KeyboardInterrupt:
        pass
    except StreamError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
