r"""Proves the sensor really embeds pictures and sound (red vs blue image, low vs high tone).
Run with Lucy Core's venv, from the Lucy_Core folder, while the embed server is up:
    .\.venv\Scripts\python.exe tools\embed_blind_check.py [--port 8092]
Exit code 0 = pass, 1 = fail. Stdlib + httpx only."""
import argparse, base64, io, math, struct, sys, wave, zlib
import httpx


def solid_png(rgb, size=64) -> bytes:
    raw = (b"\x00" + bytes(rgb) * size) * size
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def tone_wav(freq=440.0, secs=2.0, rate=16000) -> bytes:          # mono, 16 kHz, 16-bit: what the model wants
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(int(rate * secs))))
    return buf.getvalue()


def img(rgb):
    url = "data:image/png;base64," + base64.b64encode(solid_png(rgb)).decode()
    return {"content": [{"type": "image_url", "image_url": {"url": url}}]}


def aud(freq):
    b64 = base64.b64encode(tone_wav(freq)).decode()
    return {"content": [{"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}]}


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def embed(port, inputs):
    r = httpx.post(f"http://127.0.0.1:{port}/v1/embeddings", timeout=180,
                   json={"model": "stella-embed", "input": inputs, "encoding_format": "float"})
    r.raise_for_status()
    return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--no-audio", action="store_true")
    a = ap.parse_args()
    ok = True
    v = embed(a.port, [img((255, 0, 0)), img((0, 0, 255)),
                       "task: search result | query: a solid red image",
                       "task: search result | query: a solid blue image"])
    rb = dot(v[0], v[1])
    print(f"red vs blue image          : {rb:.4f}  (must be below ~0.97; 1.0000 = image is being ignored)")
    print(f"red img  -> 'red' / 'blue' : {dot(v[0], v[2]):.3f} {dot(v[0], v[3]):.3f}  (first should be larger)")
    print(f"blue img -> 'red' / 'blue' : {dot(v[1], v[2]):.3f} {dot(v[1], v[3]):.3f}  (second should be larger)")
    ok &= rb < 0.97 and dot(v[0], v[2]) > dot(v[0], v[3]) and dot(v[1], v[3]) > dot(v[1], v[2])
    if not a.no_audio:
        w = embed(a.port, [aud(440.0), aud(1760.0)])
        ab = dot(w[0], w[1])
        print(f"440 Hz vs 1760 Hz tone     : {ab:.4f}  (must be below ~0.999; audio is experimental upstream)")
        ok &= ab < 0.999
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
