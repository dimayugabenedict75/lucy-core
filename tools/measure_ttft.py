"""Time-to-first-token of a short 3-turn chat against a RUNNING Lucy Core (stdlib only, no installs).

    python tools/measure_ttft.py --label before
    python tools/measure_ttft.py --label after

Run each setting TWICE and read the second run (the first one loads/warms the model). Compare labels with:
    python tools/measure_ttft.py --compare
Results are appended to runtime/ttft_results.jsonl. To compare prompt orders, start Lucy Core with
LUCY_PROMPT_ORDER=legacy (old), static_first (default) or late, and give each run its own --label.
The three questions deliberately avoid words that make tool routing add tools (that would change the prompt).
"""
import argparse, json, os, pathlib, statistics, sys, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / "runtime" / "ttft_results.jsonl"
QUESTIONS = ["Hi! In one short sentence, define a prime number.",
             "Nice. Now give the next prime after 13, in one short sentence.",
             "Thanks! One fun fact about primes, in one short sentence."]


def one_turn(url: str, key: str, session: str, message: str, timeout: float = 300.0):
    body = json.dumps({"message": message, "session_id": session, "stream": True}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    t0 = time.perf_counter(); first = None; text = ""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data.startswith("[METRICS]") or data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("content"):
                if first is None:
                    first = time.perf_counter() - t0
                text += ev["content"]
    return first, time.perf_counter() - t0, text


def run(args):
    session = f"ttft-{int(time.time())}"
    rows = []
    for i, q in enumerate(QUESTIONS[:args.turns], 1):
        first, total, text = one_turn(args.url, args.key, session, q)
        rows.append({"turn": i, "first_token_s": None if first is None else round(first, 2), "total_s": round(total, 2), "chars": len(text)})
        print(f"turn {i}: first token {rows[-1]['first_token_s']} s, total {rows[-1]['total_s']} s, {len(text)} chars")
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"label": args.label, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "turns": rows}) + "\n")
    print(f"saved to {RESULTS} as '{args.label}'")


def compare():
    if not RESULTS.exists():
        sys.exit("no results yet")
    latest = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        r = json.loads(line); latest[r["label"]] = r            # the newest run per label
    print(f"{'label':<16}{'turn 1':>9}{'turn 2':>9}{'turn 3':>9}{'avg 2-3':>10}   (seconds to first token)")
    for label, r in latest.items():
        t = [x["first_token_s"] for x in r["turns"]]
        t += [None] * (3 - len(t))
        later = [x for x in t[1:] if x is not None]
        print(f"{label:<16}" + "".join(f"{(x if x is not None else float('nan')):>9.2f}" for x in t)
              + f"{(statistics.mean(later) if later else float('nan')):>10.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--key", default=os.environ.get("DEV_API_KEY", "dev-harness"))
    ap.add_argument("--label", default="run")
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()
    compare() if a.compare else run(a)
