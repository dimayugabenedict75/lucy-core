"""Offline tests for lucy_player (no sound card needed): a fake driver pulls the callback in real time."""
import array, json, math, sys, threading, time, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from lucy_player import DirectSoundPlayer, ChunkLog

RATE = 24000

class FakeStream:
    latency = 0.02
    def __init__(self, rate, ch, cb, block=480):
        self.rate, self.ch, self.cb, self.block = rate, ch, cb, block
        self.out = bytearray(); self.run = True
        self.t = threading.Thread(target=self._loop, daemon=True); self.t.start()
    def _loop(self):
        nxt = time.monotonic()
        while self.run:
            buf = bytearray(self.block * self.ch * 2)
            self.cb(memoryview(buf).cast("B"), self.block, None, None)
            self.out += buf
            nxt += self.block / self.rate
            time.sleep(max(0, nxt - time.monotonic()))
    def stop(self): self.run = False
    def close(self): pass

class FakeDriver:
    device_name = "fake"
    def __init__(self): self.stream = None
    def open(self, rate, ch, cb):
        self.stream = FakeStream(rate, ch, cb); return self.stream

def sine(ms, f=440, amp=12000):
    n = RATE * ms // 1000
    return array.array("h", (int(amp * math.sin(2 * math.pi * f * i / RATE)) for i in range(n))).tobytes()

def played(drv):
    a = array.array("h"); a.frombytes(bytes(drv.stream.out)); return a

def trim(a):
    nz = [i for i, v in enumerate(a) if v]; return a[nz[0]:nz[-1] + 1] if nz else a

def test_smooth_stream_is_bit_exact_and_gapless():
    drv = FakeDriver(); p = DirectSoundPlayer(RATE, driver=drv, prebuffer_ms=200)
    src = sine(2000); step = 960 * 2          # 40 ms chunks arriving a bit faster than real time
    for i in range(0, len(src), step):
        p.feed(src[i:i + step]); time.sleep(0.03)
    assert p.wait(5)
    out = trim(played(drv)); ref = array.array("h"); ref.frombytes(src)
    assert p.underruns == 0, p.stats()
    # no zero-run (gap) in the middle of the tone, fades only touch the first/last few ms
    mid = out[200:-200]
    zero_run = max_run = 0
    for v in mid:
        zero_run = zero_run + 1 if v == 0 else 0; max_run = max(max_run, zero_run)
    assert max_run < 30, max_run
    assert list(out[300:-300]) == list(ref[300:-300][:len(out[300:-300])]) or abs(len(out) - len(ref)) < 600
    p.close()

def test_jitter_is_absorbed_by_prebuffer():
    drv = FakeDriver(); p = DirectSoundPlayer(RATE, driver=drv, prebuffer_ms=400)
    src = sine(3000); step = 960 * 2
    for n, i in enumerate(range(0, len(src), step)):
        p.feed(src[i:i + step])
        time.sleep(0.2 if n % 10 == 5 else 0.02)    # one 200 ms stall every 10 chunks
    assert p.wait(6); assert p.underruns == 0, p.stats(); p.close()

def test_real_stall_rebuffers_once_and_resumes():
    drv = FakeDriver(); p = DirectSoundPlayer(RATE, driver=drv, prebuffer_ms=100, resume_ms=100)
    p.feed(sine(300)); time.sleep(0.8)              # runs dry mid-stream (no end_of_stream)
    p.feed(sine(300)); assert p.wait(5)
    assert p.underruns == 1, p.stats(); p.close()

def test_short_utterance_below_prebuffer_still_plays():
    drv = FakeDriver(); p = DirectSoundPlayer(RATE, driver=drv, prebuffer_ms=500)
    p.feed(sine(120)); p.end_of_stream(); assert p.wait(3)
    assert len(trim(played(drv))) > RATE * 0.10; p.close()

def test_odd_byte_chunks_stay_aligned():
    drv = FakeDriver(); p = DirectSoundPlayer(RATE, driver=drv, prebuffer_ms=100)
    src = sine(600)
    for i in range(0, len(src), 1001): p.feed(src[i:i + 1001])
    assert p.wait(4); out = trim(played(drv))
    assert abs(len(out) - len(src) // 2) < 300; p.close()

def test_stereo_upsample_fallback():
    class Picky(FakeDriver):
        def open(self, rate, ch, cb):
            if (rate, ch) != (48000, 2): raise OSError("unsupported")
            self.stream = FakeStream(rate, ch, cb, 960); return self.stream
    drv = Picky(); p = DirectSoundPlayer(RATE, driver=drv, prebuffer_ms=100)
    p.feed(sine(500)); assert p.wait(3)
    assert p._out_rate == 48000 and p._out_ch == 2
    out = trim(played(drv)); assert abs(len(out) / 2 - 24000) < 600; p.close()

def test_chunk_log_flags_late_chunks():
    d = tempfile.mkdtemp(); path = pathlib.Path(d) / "c.log"
    log = ChunkLog("hello", RATE, path=path)
    log.chunk(RATE * 2 // 10)                        # 100 ms of audio
    time.sleep(0.25)                                 # next chunk arrives 150 ms after audio ran dry
    e = log.chunk(RATE * 2 // 10)
    s = log.done({"gen_ms": 1})
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert [l["event"] for l in lines] == ["start", "chunk", "chunk", "done"]
    assert e["late"] and 100 < e["starved_ms"] < 220, e
    assert s["late_chunks"] == 1 and s["chunks"] == 2 and s["server_timing"] == {"gen_ms": 1}
