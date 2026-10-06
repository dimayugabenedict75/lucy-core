#!/usr/bin/env python3
"""stream_lucy_audio - zero-dependency client for Lucy Audio delta streams.

Covers every streaming route the server has:
  speak()            POST /v1/audio/speech                  (SSE speech.audio.delta)
  speak_raw()        POST /v1/audio/speech                  (stream_format=audio, raw PCM)
  transcribe_file()  POST /v1/audio/transcriptions          (multipart, stream=true)
  transcribe_live()  POST /v1/audio/transcriptions/live     (full-duplex chunked PCM in, SSE out)

CLI:
  stream_lucy_audio.py say "hello there" --model cielvox26 --rate 24000
  stream_lucy_audio.py file clip.wav --model cielvox26
  stream_lucy_audio.py listen --model voxtral-realtime
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import IO, Iterable, Iterator
from urllib.parse import urlencode, urlsplit


class StreamError(RuntimeError):
    """Server sent an error event, replied badly, or the stream ended without [DONE]."""


# -- SSE ----------------------------------------------------------------------

def iter_sse_chunks(chunks: Iterable[bytes]) -> Iterator[dict]:
    """Spec-correct SSE decoder over byte chunks: multi-line data, comments, CRLF, [DONE] sentinel.
    UTF-8 is decoded per COMPLETE line, so a character split across reads is safe.

    Raises StreamError on {"type":"error"}, on malformed JSON, and on EOF before [DONE] - the server
    emits an error event and then just closes, so a silent EOF means truncation.
    """
    buf = b""
    data: list[str] = []
    for chunk in chunks:
        buf += chunk
        while (nl := buf.find(b"\n")) >= 0:
            raw, buf = buf[:nl], buf[nl + 1:]
            line = raw.decode("utf-8", "replace").rstrip("\r")
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
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                raise StreamError(f"malformed event payload: {payload[:120]!r}") from None
            if event.get("type") == "error":
                err = event.get("error")
                raise StreamError(str(err.get("message") if isinstance(err, dict) else err or "unknown server error"))
            yield event
    raise StreamError("stream closed before [DONE] (truncated)")


def iter_sse(resp: IO[bytes]) -> Iterator[dict]:
    """SSE events off an http.client response (read1: whatever has arrived, no waiting to fill a buffer)."""
    def chunks() -> Iterator[bytes]:
        try:
            while buf := resp.read1(65536):
                yield buf
        except (http.client.HTTPException, OSError) as e:   # IncompleteRead: chunked body dropped mid-stream
            raise StreamError(f"stream closed before [DONE] (truncated): {e!r}") from e
    return iter_sse_chunks(chunks())


# -- HTTP plumbing -----------------------------------------------------------

class LucyAudioClient:
    """Zero-dependency client for Lucy Audio's streaming TTS and ASR endpoints."""

    def __init__(self, base_url: str = "http://127.0.0.1:8091", timeout: float = 120.0):
        u = urlsplit(base_url)
        self.https = u.scheme == "https"
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.https else 80)
        self.timeout = timeout      # per-read idle timeout for TTS / file routes; live ASR uses its own rules
        self.last_timing: dict | None = None   # `timing` of the last finished speak(); None until one completes

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
                body = body[:500]
            raise StreamError(f"HTTP {resp.status}: {body}")

    def _post(self, path: str, body: bytes, ctype: str, accept: str) -> tuple:
        conn = self._conn()
        try:
            conn.request("POST", path, body=body,
                         headers={"Content-Type": ctype, "Accept": accept})
            resp = conn.getresponse()
            self._check(resp)
        except BaseException:
            conn.close()            # don't leak the socket on an HTTP error / refused connection
            raise
        return conn, resp

# -- TTS ---------------------------------------------------------------------

    def speak(self, text: str, model: str, **extra) -> Iterator[bytes]:
        """Yield s16LE PCM chunks as they're generated (SSE mode)."""
        body = json.dumps({"model": model, "input": text, "response_format": "pcm",
                           "stream_format": "sse", "sample_rate": 24000, **extra}).encode()
        conn, resp = self._post("/v1/audio/speech", body, "application/json",
                                "text/event-stream")
        self.last_timing = None
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
                           "stream_format": "sse", "sample_rate": 24000, **extra}).encode()
        conn, resp = self._post("/v1/audio/speech", body, "application/json",
                                "application/octet-stream")
        carry = b""
        try:
            while buf := resp.read1(chunk):
                buf, carry = carry + buf, b""
                if len(buf) % 2:
                    # HTTP chunks can split a sample
                    buf, carry = buf[:-1], buf[-1:]
                if buf:                      # a lone odd byte leaves nothing to play yet
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
                        language: str | None = None, idle_timeout: float = 60.0) -> Iterator[dict]:
        """Full duplex: a thread pushes chunked PCM while we read SSE on the same socket.

        Deltas can show up while you're still talking (model permitting).

        Timeouts: the socket never times out by itself (a quiet speaker is not an error). Only once
        the upload has FINISHED does a server that then stays silent for `idle_timeout` seconds raise.
        """
        q = {"model": model, "sample_rate": sample_rate, "channels": channels,
             "sample_format": sample_format}
        if language:
            q["language"] = language
        conn = self._conn()                      # self.timeout bounds the connect only
        send_err: list[BaseException] = []
        stop = threading.Event()
        closing = threading.Event()
        upload_done_at: list[float] = []         # set (one item) when the pump ends
        last_rx = [time.monotonic()]
        timed_out: list[bool] = []

        def shut() -> None:
            with contextlib.suppress(OSError):
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)   # unblocks a read stuck in another thread

        def pump() -> None:
            try:
                for chunk in pcm:
                    if stop.is_set():
                        return
                    if chunk:
                        conn.send(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                conn.send(b"0\r\n\r\n")  # terminating chunk - "speaker done"
            except BaseException as e:       # noqa: BLE001 - surfaced below
                send_err.append(e)
            finally:
                upload_done_at.append(time.monotonic())
                close = getattr(pcm, "close", None)    # e.g. mic(): its finally reaps the recorder
                if close:
                    with contextlib.suppress(Exception):
                        close()

        def watchdog() -> None:
            while not closing.wait(0.25):
                if upload_done_at and time.monotonic() - max(last_rx[0], upload_done_at[0]) > idle_timeout:
                    timed_out.append(True)
                    shut()
                    return

        t = None
        try:
            conn.connect()
            conn.sock.settimeout(None)
            conn.putrequest("POST", "/v1/audio/transcriptions/live?" + urlencode(q),
                            skip_accept_encoding=True)
            conn.putheader("Transfer-Encoding", "chunked")  # no Expect: 100-continue
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Accept", "text/event-stream")
            conn.endheaders()
            threading.Thread(target=watchdog, daemon=True).start()
            t = threading.Thread(target=pump, daemon=True)
            t.start()
            resp = conn.getresponse()   # headers arrive before the body finishes
            self._check(resp)

            def chunks() -> Iterator[bytes]:
                while buf := resp.read1(65536):
                    last_rx[0] = time.monotonic()
                    yield buf

            yield from iter_sse_chunks(chunks())
        except (StreamError, OSError, http.client.HTTPException) as e:
            if timed_out:
                raise StreamError(f"timed out: no reply for {idle_timeout:.0f}s after the audio ended") from e
            transport = not isinstance(e, StreamError) or "truncated" in str(e)
            if transport and t is not None and not send_err:
                t.join(timeout=1.0)      # a dead server shows up as a failed send a moment later
            if send_err and transport:
                raise StreamError(f"upload failed: {send_err[0]}") from send_err[0]
            if isinstance(e, http.client.HTTPException):
                raise StreamError(f"live transcription failed: {e}") from e
            raise
        finally:
            stop.set()
            closing.set()
            shut()
            conn.close()


# -- Transcript assembly -----------------------------------------------------

@dataclass
class Transcript:
    """Appends deltas; swaps in the server's authoritative text on done.

    `drifted` is True when the concatenated deltas didn't match the final text (ignoring
    whitespace at the ends) - the decoder revised words it had already published. A server that
    sends only `done` is not drift. A delta carrying a byte `offset` (patched server) truncates
    the text at that offset first, multi-byte safe.
    """
    text: str = ""
    final: bool = False
    drifted: bool = False
    deltas: int = 0
    timing: dict = field(default_factory=dict)

    def feed(self, ev: dict) -> str | None:
        kind = ev.get("type")
        if kind == "transcript.text.delta":
            offset = ev.get("offset")
            if isinstance(offset, int) and offset >= 0:
                self.text = self.text.encode()[:offset].decode("utf-8", "ignore")
            self.text += ev.get("delta", "")
            self.deltas += 1
            return ev.get("delta", "")
        if kind == "transcript.text.done":
            final = ev.get("text")
            if isinstance(final, str):           # a done without text keeps the deltas
                self.drifted = self.deltas > 0 and final.strip() != self.text.strip()
                self.text = final
            self.final = True
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
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()


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
    c = LucyAudioClient(a.url)

    try:
        if a.cmd == "say":
            gen = (c.speak_raw if a.raw else c.speak)(a.text, a.model)
            proc = None if a.out else player(a.rate)
            sink = open(a.out, "wb") if a.out else proc.stdin
            try:
                for pcm in gen:
                    sink.write(pcm); sink.flush()
            except BrokenPipeError:
                sys.exit("error: the audio player exited early")
            finally:
                with contextlib.suppress(BrokenPipeError):
                    sink.close()
                if proc is not None:
                    proc.wait()          # let the tail of the audio finish playing
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
    except (OSError, RuntimeError) as e:     # refused connection, missing player / recorder, ...
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
