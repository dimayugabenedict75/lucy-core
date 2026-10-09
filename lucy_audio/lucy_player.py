#!/usr/bin/env python3
"""lucy_player - gapless Windows DirectSound playback + delta-chunk logging for Lucy Audio.

Two independent pieces, both usable from the CLI client and from the Lucy Core server:

  ChunkLog            one JSON line per `speech.audio.delta` chunk (arrival time, gap, size, audio
                      length, how far ahead of real-time the stream is, predicted underrun) plus a
                      summary line per stream (TTFB, real-time factor, late chunks, ...).

  DirectSoundPlayer   a pull-model (callback) player on the Windows DirectSound host API, with a
                      jitter buffer. The audio device keeps running; the callback takes bytes from
                      the buffer, so playback never depends on when Python manages to write.
                      Nothing starts until `prebuffer_ms` of audio is queued, a real underrun
                      re-buffers instead of stuttering, and every start/stop is faded (a few ms)
                      so there are no clicks at the seams.

  make_player()       DirectSound when available (Windows + `pip install sounddevice`), otherwise
                      the old pipe-to-ffplay/aplay/pw-cat player, same interface.

Log file: <Lucy_Core>/runtime/logs/deltastream_chunks.log (override with LUCY_CHUNK_LOG).
"""

from __future__ import annotations

import array
import collections
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parents[1] / "runtime" / "logs" / "deltastream_chunks.log"


# =============================================================================
# Chunk logging
# =============================================================================

class ChunkLog:
    """Per-stream JSONL logger for the delta (SSE) chunks of one TTS request.

    Events written (all carry `ts`, `stream`, `event`):
      start   text preview, sample rate, source (cli / server)
      chunk   seq, bytes, audio_ms, t_ms (since request), gap_ms (since previous chunk),
              audio_total_ms, lead_ms (audio delivered minus wall time since the first chunk;
              < 0 means the generator is slower than real-time), late (the previous audio would
              have run dry before this chunk arrived), starved_ms (how long), buffered_ms (player)
      player  events from DirectSoundPlayer (underrun, device status, ...)
      done    summary: chunks, audio_ms, ttfb_ms, total_ms, rtf, min_lead_ms, max_gap_ms,
              late_chunks, starved_ms, player stats, server `timing`
      error   message
    """

    _lock = threading.Lock()

    def __init__(self, text: str = "", rate: int = 24000, channels: int = 1, source: str = "cli",
                 path: str | os.PathLike | None = None, player=None, enabled: bool = True,
                 stream_id: str | None = None, **meta):
        self.enabled = enabled
        self.path = Path(path or os.environ.get("LUCY_CHUNK_LOG") or DEFAULT_LOG)
        self.id = stream_id or uuid.uuid4().hex[:8]
        self.rate = rate
        self._bpf = 2 * channels
        self.player = player
        self.t0 = time.monotonic()
        self.t_first: float | None = None
        self.t_prev: float | None = None
        self._base_t = 0.0                  # playback-clock baseline; reset after every starvation
        self._base_audio = 0.0
        self.seq = 0
        self.bytes = 0
        self.audio_ms = 0.0
        self.late = 0
        self.starved_ms = 0.0
        self.max_gap_ms = 0.0
        self.min_lead_ms: float | None = None
        self._finished = False
        self._write({"event": "start", "source": source, "rate": rate, "text_chars": len(text),
                     "text_preview": text[:80] + ("..." if len(text) > 80 else ""), **meta})

    # -- writing ---------------------------------------------------------------
    def _write(self, entry: dict) -> None:
        if not self.enabled:
            return
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}",
                 "stream": self.id, **entry}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass                                    # logging must never break playback

    # -- events ----------------------------------------------------------------
    def chunk(self, nbytes: int) -> dict:
        """Record one delta chunk of `nbytes` s16LE PCM. Returns the logged entry."""
        now = time.monotonic()
        dur_ms = nbytes / self._bpf / self.rate * 1000.0
        t_ms = (now - self.t0) * 1000.0
        gap_ms = None if self.t_prev is None else (now - self.t_prev) * 1000.0
        if self.t_first is None:
            self.t_first = self._base_t = now
            lead_before = 0.0
        else:
            lead_before = (self.audio_ms - self._base_audio) - (now - self._base_t) * 1000.0
        late = self.seq > 0 and lead_before < 0
        starved = -lead_before if late else 0.0
        if late:
            self.late += 1
            self.starved_ms += -lead_before
            self._base_t, self._base_audio = now, self.audio_ms   # playback restarts at this chunk
            lead_before = 0.0
        self.seq += 1
        self.bytes += nbytes
        self.audio_ms += dur_ms
        self.t_prev = now
        lead_after = lead_before + dur_ms
        if self.seq > 1:
            self.min_lead_ms = lead_after if self.min_lead_ms is None else min(self.min_lead_ms, lead_after)
        if gap_ms is not None:
            self.max_gap_ms = max(self.max_gap_ms, gap_ms)
        entry = {"event": "chunk", "seq": self.seq, "bytes": nbytes, "audio_ms": round(dur_ms, 1),
                 "t_ms": round(t_ms, 1),
                 "gap_ms": None if gap_ms is None else round(gap_ms, 1),
                 "audio_total_ms": round(self.audio_ms, 1), "lead_ms": round(lead_after, 1),
                 "late": late, "starved_ms": round(starved, 1)}
        if nbytes % self._bpf:
            entry["misaligned"] = True              # chunk split a sample: a bug upstream
        if self.player is not None:
            with contextlib.suppress(Exception):
                entry["buffered_ms"] = round(self.player.buffered_ms, 1)
        self._write(entry)
        return entry

    def player_event(self, ev: dict) -> None:
        self._write({"event": "player", **ev})

    def done(self, server_timing: dict | None = None) -> dict:
        """Write the summary line once (further calls are ignored)."""
        if self._finished:
            return {}
        self._finished = True
        now = time.monotonic()
        gen_s = (self.t_prev - self.t_first) if (self.t_first and self.t_prev) else 0.0
        summary = {"event": "done", "chunks": self.seq, "bytes": self.bytes,
                   "audio_ms": round(self.audio_ms, 1),
                   "ttfb_ms": None if self.t_first is None else round((self.t_first - self.t0) * 1000, 1),
                   "total_ms": round((now - self.t0) * 1000, 1),
                   # generation time (first->last chunk) / audio length. < 1.0 = faster than real time
                   "rtf": round(gen_s / (self.audio_ms / 1000.0), 3) if self.audio_ms else None,
                   "min_lead_ms": None if self.min_lead_ms is None else round(self.min_lead_ms, 1),
                   "max_gap_ms": round(self.max_gap_ms, 1), "late_chunks": self.late,
                   "starved_ms": round(self.starved_ms, 1)}
        if self.player is not None:
            with contextlib.suppress(Exception):
                summary["player"] = self.player.stats()
        if server_timing:
            summary["server_timing"] = server_timing
        self._write(summary)
        return summary

    def error(self, message: str) -> None:
        self._finished = True
        self._write({"event": "error", "message": str(message)[:500], "chunks": self.seq})


# =============================================================================
# DirectSound player
# =============================================================================

_IDLE, _BUFFERING, _PLAYING = "idle", "buffering", "playing"


class _SoundDeviceDriver:
    """Opens a callback output stream on the Windows DirectSound host API via PortAudio."""

    def __init__(self, hostapi: str = "DirectSound", device: int | str | None = None,
                 latency: float | str = "high"):
        self.hostapi, self.device, self.latency = hostapi, device, latency
        self.device_name = None

    def open(self, rate: int, channels: int, callback):
        try:
            import sounddevice as sd
        except ImportError as e:
            raise RuntimeError("sounddevice is not installed: pip install sounddevice") from e
        dev = self.device
        if dev is None:
            for api in sd.query_hostapis():
                if self.hostapi.lower() in api["name"].lower() and api["default_output_device"] >= 0:
                    dev = api["default_output_device"]
                    break
        if dev is not None:
            self.device_name = sd.query_devices(dev)["name"]
        stream = sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16", device=dev,
                                    latency=self.latency, callback=callback)
        stream.start()
        return stream


class DirectSoundPlayer:
    """Gapless s16LE PCM player: persistent device stream + jitter buffer.

    feed(pcm)            queue audio (any chunk size, non-blocking)
    end_of_stream()      "no more audio for this utterance": play what's queued even if it is
                         shorter than the prebuffer, then go idle
    wait(timeout)        block until everything queued has been played
    flush()              drop queued audio (barge-in / stop)
    close()              release the device
    """

    def __init__(self, rate: int = 24000, channels: int = 1, prebuffer_ms: int = 300,
                 resume_ms: int = 200, fade_ms: float = 4.0, idle_close_s: float = 5.0,
                 max_buffer_s: float = 120.0, on_event=None, driver=None, **driver_kw):
        if sys.byteorder != "little":
            raise RuntimeError("s16LE playback needs a little-endian host")
        self.rate, self.channels = rate, channels
        self.prebuffer_ms, self.resume_ms, self.fade_ms = prebuffer_ms, resume_ms, fade_ms
        self.idle_close_s, self.max_buffer_s = idle_close_s, max_buffer_s
        self.on_event = on_event
        self._driver = driver or _SoundDeviceDriver(**driver_kw)
        self._stream = None
        self._out_rate, self._out_ch, self._bpf = rate, channels, 2 * channels
        self._q: collections.deque[bytes] = collections.deque()
        self._head = 0                                   # read offset into _q[0]
        self._buffered = 0                               # bytes queued (output format)
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._open_lock = threading.Lock()
        self._state = _IDLE
        self._threshold = 0
        self._fade_in = False
        self._eos = False
        self._carry = b""
        self._last_in = 0                                # last input sample (resampler continuity)
        self._events: collections.deque[dict] = collections.deque()
        self._latency_s = 0.2
        self._last_activity = time.monotonic()
        self._closed = False
        self._reaper: threading.Thread | None = None
        # stats
        self.underruns = 0
        self.underrun_events: list[dict] = []
        self.status_flags = 0
        self.fed_ms = 0.0
        self.played_ms = 0.0
        self.max_buffered_ms = 0.0
        self.dropped_ms = 0.0
        self.device_name: str | None = None

    # -- introspection --------------------------------------------------------
    @property
    def buffered_ms(self) -> float:
        return self._buffered / (self._out_rate * self._bpf) * 1000.0

    @property
    def device(self) -> str:
        return (f"{self.device_name or 'default'} @ {self._out_rate} Hz x{self._out_ch}"
                if self._stream else "closed")

    def stats(self) -> dict:
        return {"underruns": self.underruns, "fed_ms": round(self.fed_ms, 1),
                "played_ms": round(self.played_ms, 1), "max_buffered_ms": round(self.max_buffered_ms, 1),
                "dropped_ms": round(self.dropped_ms, 1), "status_flags": self.status_flags,
                "device": self.device}

    # -- device ---------------------------------------------------------------
    def open(self) -> None:
        """Open the device stream now (so the first feed() doesn't pay for it)."""
        with self._open_lock:
            if self._stream is not None:
                return
            self._closed = False
            # DirectSound mixes/resamples, but if a device refuses 24 kHz mono, fall back to
            # stereo and/or an integer upsample (done in feed()).
            configs = [(self.rate, self.channels)]
            if self.channels == 1:
                configs.append((self.rate, 2))
            configs += [(self.rate * 2, c) for _, c in list(configs)]
            last: Exception | None = None
            for out_rate, out_ch in configs:
                self._out_rate, self._out_ch, self._bpf = out_rate, out_ch, 2 * out_ch
                try:
                    self._stream = self._driver.open(out_rate, out_ch, self._callback)
                    break
                except Exception as e:                  # noqa: BLE001 - try the next layout
                    last = e
            else:
                raise RuntimeError(f"could not open DirectSound output: {last}") from last
            self.device_name = getattr(self._driver, "device_name", None)
            lat = getattr(self._stream, "latency", None)
            with contextlib.suppress(Exception):
                self._latency_s = float(lat[0] if isinstance(lat, (tuple, list)) else lat)
            self._last_activity = time.monotonic()
            self._events.append({"type": "open", "device": self.device, "latency_ms": round(self._latency_s * 1000)})
            if self._reaper is None or not self._reaper.is_alive():
                self._reaper = threading.Thread(target=self._reap, daemon=True, name="lucy-player-reaper")
                self._reaper.start()

    def _prebuf_bytes(self, ms: float) -> int:
        frames = int(self._out_rate * ms / 1000.0)
        return frames * self._bpf

    # -- input ----------------------------------------------------------------
    def _convert(self, pcm: bytes) -> bytes:
        """Input format (rate, channels) -> device format (rare fallback path)."""
        if self._out_rate == self.rate and self._out_ch == self.channels:
            return pcm
        a = array.array("h")
        a.frombytes(pcm)
        k = self._out_rate // self.rate
        if k > 1:                                        # linear-interpolated integer upsample (mono)
            up = array.array("h", bytes(len(a) * k * 2))
            prev = self._last_in
            for i, cur in enumerate(a):
                base = i * k
                for j in range(k):
                    up[base + j] = prev + (cur - prev) * (j + 1) // k
                prev = cur
            self._last_in = prev
            a = up
        if self._out_ch != self.channels:                # mono -> N channels
            wide = array.array("h", bytes(len(a) * self._out_ch * 2))
            for c in range(self._out_ch):
                wide[c::self._out_ch] = a
            a = wide
        return a.tobytes()

    def feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        if self._stream is None:
            self.open()
        pcm = self._carry + pcm
        frame = 2 * self.channels
        cut = len(pcm) - len(pcm) % frame                # never split a sample/frame
        pcm, self._carry = pcm[:cut], pcm[cut:]
        if not pcm:
            return
        out = self._convert(pcm)
        with self._cv:
            if self._buffered + len(out) > self._prebuf_bytes(self.max_buffer_s * 1000):
                self.dropped_ms += len(out) / (self._out_rate * self._bpf) * 1000.0
                return
            self._eos = False
            if self._state == _IDLE:
                self._state, self._threshold = _BUFFERING, self._prebuf_bytes(self.prebuffer_ms)
            self._q.append(out)
            self._buffered += len(out)
            self.fed_ms += len(pcm) / (self.rate * frame) * 1000.0
            self.max_buffered_ms = max(self.max_buffered_ms, self.buffered_ms)
            self._last_activity = time.monotonic()

    def end_of_stream(self) -> None:
        with self._cv:
            self._eos = True
            self._last_activity = time.monotonic()

    def flush(self) -> None:
        with self._cv:
            self._q.clear()
            self._head = self._buffered = 0
            self._state, self._eos = _IDLE, False
            self._cv.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until all queued audio has been played. True if it finished."""
        self.end_of_stream()
        with self._cv:
            ok = self._cv.wait_for(lambda: self._state == _IDLE and self._buffered == 0, timeout)
        if ok and self._stream is not None:
            time.sleep(self._latency_s)                  # let the device buffer drain, don't clip the tail
        self.drain_events()
        return ok

    # -- audio callback (runs on PortAudio's thread: no I/O, no allocation-heavy work) ---------
    def _take(self, n: int) -> bytes:
        parts, left = [], n
        while left and self._q:
            head = self._q[0]
            avail = len(head) - self._head
            if avail <= left:
                parts.append(head[self._head:] if self._head else head)
                self._q.popleft()
                self._head = 0
                left -= avail
            else:
                parts.append(head[self._head:self._head + left])
                self._head += left
                left = 0
        data = b"".join(parts)
        self._buffered -= len(data)
        return data

    def _ramp(self, data: bytes, up: bool) -> bytes:
        frames = max(1, int(self._out_rate * self.fade_ms / 1000.0))
        frames = min(frames, len(data) // self._bpf)
        if frames < 2:
            return data
        a = array.array("h")
        a.frombytes(data)
        ch, n = self._out_ch, frames
        rng = range(n) if up else range(len(a) // ch - n, len(a) // ch)
        for k, i in enumerate(rng):
            g = k / (n - 1) if up else 1.0 - k / (n - 1)
            for c in range(ch):
                a[i * ch + c] = int(a[i * ch + c] * g)
        return a.tobytes()

    def _callback(self, outdata, frames, time_info, status) -> None:
        need = frames * self._bpf
        with self._cv:
            if status:
                self.status_flags += 1
                self._events.append({"type": "device_status", "status": str(status)})
            out = self._render(need)
        outdata[:need] = out

    def _render(self, need: int) -> bytes:
        if self._state == _BUFFERING:
            if self._buffered >= self._threshold or (self._eos and self._buffered > 0):
                self._state, self._fade_in = _PLAYING, True
            else:
                return bytes(need)
        if self._state != _PLAYING:
            return bytes(need)
        data = self._take(min(need, self._buffered))
        if data and self._fade_in:
            data, self._fade_in = self._ramp(data, up=True), False
        self.played_ms += len(data) / (self._out_rate * self._bpf) * 1000.0
        if len(data) < need:                             # ran dry
            if data:
                data = self._ramp(data, up=False)
            if self._eos:
                self._state = _IDLE                      # natural end of the utterance
                self._cv.notify_all()
            else:                                        # real underrun: re-buffer, don't stutter
                self.underruns += 1
                ev = {"type": "underrun", "n": self.underruns, "played_ms": round(self.played_ms, 1),
                      "short_ms": round((need - len(data)) / (self._out_rate * self._bpf) * 1000.0, 1)}
                self.underrun_events.append(ev)
                self._events.append(ev)
                self._state, self._threshold = _BUFFERING, self._prebuf_bytes(self.resume_ms)
            data += bytes(need - len(data))
        return data

    # -- housekeeping ---------------------------------------------------------
    def drain_events(self) -> None:
        while self._events:
            ev = self._events.popleft()
            if self.on_event:
                with contextlib.suppress(Exception):
                    self.on_event(ev)

    def _reap(self) -> None:
        """Forward player events to the log and release the device after idle_close_s of silence."""
        while not self._closed:
            time.sleep(0.2)
            self.drain_events()
            with self._lock:
                idle = (self._state == _IDLE and self._buffered == 0
                        and time.monotonic() - self._last_activity > self.idle_close_s)
            if idle and self.idle_close_s > 0:
                self._release()
                return

    def _release(self) -> None:
        with self._open_lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
        self.drain_events()

    def close(self) -> None:
        self._closed = True
        self._release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.wait()
        self.close()


# =============================================================================
# Fallback: pipe to an external player (previous behaviour)
# =============================================================================

class PipePlayer:
    """Same interface as DirectSoundPlayer, backed by pw-cat / aplay / ffplay on stdin."""

    def __init__(self, rate: int = 24000, channels: int = 1, **_ignored):
        self.rate, self.channels = rate, channels
        self.proc: subprocess.Popen | None = None
        self.name = None
        self.buffered_ms = 0.0
        self.underruns = 0

    def open(self) -> None:
        if self.proc:
            return
        r, c = str(self.rate), str(self.channels)
        for argv in (["pw-cat", "--playback", "--format", "s16", "--rate", r, "--channels", c, "-"],
                     ["aplay", "-q", "-f", "S16_LE", "-r", r, "-c", c],
                     ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-fflags", "nobuffer",
                      "-f", "s16le", "-ar", r, "-ch_layout", "mono" if self.channels == 1 else "stereo", "-"]):
            if shutil.which(argv[0]):
                self.name = argv[0]
                self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE)
                return
        raise RuntimeError("need pw-cat, aplay or ffplay (or: pip install sounddevice on Windows)")

    @property
    def device(self) -> str:
        return self.name or "closed"

    def feed(self, pcm: bytes) -> None:
        self.open()
        self.proc.stdin.write(pcm)
        self.proc.stdin.flush()

    def end_of_stream(self) -> None:
        if self.proc and self.proc.stdin:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()

    def wait(self, timeout: float | None = None) -> bool:
        self.end_of_stream()
        if self.proc:
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                return False
        return True

    def flush(self) -> None:
        self.close()

    def stats(self) -> dict:
        return {"player": self.name}

    def drain_events(self) -> None:
        pass

    def close(self) -> None:
        if self.proc:
            with contextlib.suppress(Exception):
                self.proc.terminate()
            self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.wait()
        self.close()


def directsound_available() -> bool:
    """True on Windows when sounddevice (PortAudio) is installed and exposes DirectSound."""
    if sys.platform != "win32":
        return False
    try:
        import sounddevice as sd
        return any("directsound" in api["name"].lower() for api in sd.query_hostapis())
    except Exception:                                    # noqa: BLE001
        return False


def make_player(rate: int = 24000, channels: int = 1, prefer: str = "auto", **kw):
    """prefer: 'auto' | 'directsound' (error if unavailable) | 'pipe' (ffplay/aplay/pw-cat)."""
    if prefer != "pipe" and directsound_available():
        return DirectSoundPlayer(rate, channels, **kw)
    if prefer == "directsound":
        raise RuntimeError("DirectSound unavailable: needs Windows and `pip install sounddevice`")
    return PipePlayer(rate, channels)
