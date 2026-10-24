r"""Picks a cosine threshold from YOUR data. Run with Lucy Core's venv while the embed server is up:
    .\.venv\Scripts\python.exe tools\embed_calibrate.py related.txt unrelated.txt [--port 8092]
Each file has one pair per line:  first text ||| second text
  related.txt   pairs that SHOULD match (a memory and a question that needs it)
  unrelated.txt pairs that should NOT match (same topic area, wrong answer is the useful kind)
Pairs are embedded the way retrieval does it: first text as a query, second as a document."""
import argparse, sys
import httpx


def read_pairs(path):
    out = []
    for line in open(path, encoding="utf-8"):
        if "|||" in line:
            a, b = (s.strip() for s in line.split("|||", 1))
            if a and b:
                out.append((a, b))
    return out


def cosines(port, pairs):
    inputs = []
    for q, d in pairs:
        inputs += [f"task: search result | query: {q}", f"title: none | text: {d}"]
    r = httpx.post(f"http://127.0.0.1:{port}/v1/embeddings", timeout=180,
                   json={"model": "stella-embed", "input": inputs, "encoding_format": "float"})
    r.raise_for_status()
    v = [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
    return [sum(x * y for x, y in zip(v[i], v[i + 1])) for i in range(0, len(v), 2)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("related"); ap.add_argument("unrelated")
    ap.add_argument("--port", type=int, default=8092)
    a = ap.parse_args()
    rel, unr = cosines(a.port, read_pairs(a.related)), cosines(a.port, read_pairs(a.unrelated))
    if not rel or not unr:
        print("need at least one pair in each file"); return 1
    weakest_rel, strongest_unr = min(rel), max(unr)
    gap = weakest_rel - strongest_unr
    print(f"related   : n={len(rel)}  min={weakest_rel:.3f}  mean={sum(rel)/len(rel):.3f}")
    print(f"unrelated : n={len(unr)}  max={strongest_unr:.3f}  mean={sum(unr)/len(unr):.3f}")
    print(f"gap (weakest related - strongest unrelated) = {gap:.3f}")
    if gap > 0:
        print(f"separable: True   threshold ~ {(weakest_rel + strongest_unr) / 2:.3f}")
    else:
        print(f"separable: False  (any threshold loses some related pairs; best compromise ~ {(weakest_rel + strongest_unr) / 2:.3f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
