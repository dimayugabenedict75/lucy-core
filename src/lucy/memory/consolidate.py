"""Memory consolidation: keep Lucy's long-term memory tidy without ever losing something for good.

Runs in the nightly/idle reflection job (no model needed) and can be run by hand. Three jobs:

  1. DEDUPE / MERGE   near-identical memories (TF-IDF cosine >= MERGE_SCORE) are merged into one: the survivor keeps the
                      higher importance and the summed use count, the other is ARCHIVED (status 'archived',
                      merged_into=<survivor id>), not deleted. Two similar memories that disagree (see below) are
                      flagged instead of merged.
  2. CONTRADICTIONS   similar memories (>= FLAG_SCORE) in the same category that look opposite (a "not"/"never" on one
                      side, a different number/day/month, like vs dislike...) get a flag "may conflict with #N".
                      Heuristic on purpose: it only raises a hand; the daily note lists them and the model/user resolves
                      them with `remember(replaces_id=...)`, which clears the flag. Flags are recomputed every run.
  3. DECAY            memories nobody has used for DECAY_DAYS lose 1 importance (at most once per DECAY_DAYS, never below 1,
                      never for preferences/standing instructions, never for importance 5). Importance-1 memories unused for
                      STALE_DAYS are archived.

Everything is reversible: archived rows stay in the database (`Conductor.unarchive`).
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Tuple

PINNED_CATEGORIES = ("preference", "instruction")
MERGE_SCORE = 0.75
FLAG_SCORE = 0.35
DECAY_DAYS = 45
STALE_DAYS = 180

_NEG = re.compile(r"\b(?:not|no|never|none|nothing|nobody|cannot|can't|cant|won't|wont|don't|dont|doesn't|doesnt|didn't|didnt|"
                  r"isn't|isnt|aren't|arent|wasn't|wasnt|no longer|stopped|quit|without)\b", re.IGNORECASE)
_NUM = re.compile(r"\d+(?:[.:]\d+)?")
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
         "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_OPPOSITES = [("like", "dislike"), ("likes", "dislikes"), ("love", "hate"), ("loves", "hates"), ("prefer", "avoid"),
              ("prefers", "avoids"), ("always", "never"), ("enable", "disable"), ("enabled", "disabled"), ("yes", "no"),
              ("on", "off"), ("morning", "evening"), ("short", "long"), ("brief", "detailed"), ("formal", "casual"),
              ("tea", "coffee"), ("dark", "light"), ("before", "after")]


def _words(t: str) -> set:
    return set(re.findall(r"[a-z']+", t.lower()))


_STOP = {"the", "a", "an", "in", "of", "to", "is", "are", "am", "was", "his", "her", "my", "i", "he", "she", "it", "at", "on",
         "for", "and", "with", "that", "this", "be", "do", "does", "did", "has", "have", "had", "very", "really"}


def _stem(w: str) -> str:
    w = w.replace("'s", "")
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    return w


def _content(t: str) -> set:
    """Stemmed words that carry meaning (no filler, no negations: those are compared separately)."""
    return {_stem(w) for w in _words(t) if w not in _STOP and not _NEG.fullmatch(w)}


def overlap(a: str, b: str) -> float:
    """Jaccard overlap of the meaningful (stemmed) words. TF-IDF scores are low on short opposite statements
    ("likes tea" / "doesn't like tea"), so conflict candidates use max(TF-IDF, this)."""
    ca, cb = _content(a), _content(b)
    return len(ca & cb) / len(ca | cb) if ca and cb else 0.0


def conflicting(a: str, b: str) -> bool:
    """Do two similar statements look like they disagree? (heuristic, errs on the side of raising a hand)"""
    la, lb = a.lower(), b.lower()
    if bool(_NEG.search(la)) != bool(_NEG.search(lb)):
        return True
    if set(_NUM.findall(la)) != set(_NUM.findall(lb)):
        return True
    wa, wb = _words(a), _words(b)
    if any((d in wa) != (d in wb) for d in _DAYS):
        return True
    for x, y in _OPPOSITES:
        if (x in wa and y in wb and x not in wb and y not in wa) or (y in wa and x in wb and y not in wb and x not in wa):
            return True
    return False


def _age_days(doc: dict, now: float) -> Optional[float]:
    """Days since the memory was last used/updated/created (None when it has no date: such rows never decay)."""
    for k in ("last_used", "updated", "created"):
        v = doc.get(k)
        if v:
            try:
                return (now - time.mktime(time.strptime(v, "%Y-%m-%d %H:%M:%S"))) / 86400.0
            except (TypeError, ValueError):
                continue
    return None


def _survivor(a: dict, b: dict) -> Tuple[dict, dict]:
    """(keep, drop): higher importance, then more uses, then the more recently updated one."""
    ka = (a.get("importance", 3), a.get("uses", 0), a.get("updated") or a.get("created") or "")
    kb = (b.get("importance", 3), b.get("uses", 0), b.get("updated") or b.get("created") or "")
    return (a, b) if ka >= kb else (b, a)


def consolidate(conductor, now: Optional[float] = None, dry_run: bool = False) -> dict:
    """Tidy the memory. Returns {merged: [(dropped_id, kept_id)], flagged: [(id, id)], decayed: [id], archived: [id]}."""
    now = time.time() if now is None else now
    report: dict = {"merged": [], "flagged": [], "decayed": [], "archived": []}
    with conductor._lock:
        docs = conductor.knowledge_store
        if len(docs) >= 2:
            sim = conductor.embedding_engine.similarity_matrix([d["text"] for d in docs])
        else:
            sim = None
        # Stella (meaning-based) similarity on top of TF-IDF, only when every memory already has a vector.
        nsim = conductor.neural_sims() if hasattr(conductor, "neural_sims") else None
        n_merge, n_flag = getattr(conductor, "NEURAL_MERGE", 0.95), getattr(conductor, "NEURAL_FLAG", 0.78)
        report["mode"] = "neural+tfidf" if nsim is not None else "tfidf"
        gone: Dict[int, int] = {}                                   # dropped id -> kept id
        flagged_pairs: List[Tuple[int, int]] = []
        n = len(docs)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = docs[i], docs[j]
                if a["id"] in gone or b["id"] in gone:
                    continue
                s = float(sim[i][j])
                sn = float(nsim[i][j]) if nsim is not None else 0.0
                cand = max(s, overlap(a["text"], b["text"]))
                if cand < FLAG_SCORE and sn < n_flag:
                    continue
                if conflicting(a["text"], b["text"]):
                    if a.get("category") == b.get("category") or cand >= MERGE_SCORE or sn >= n_flag:
                        flagged_pairs.append((a["id"], b["id"]))
                    continue
                if s >= MERGE_SCORE or sn >= n_merge:
                    keep, drop = _survivor(a, b)
                    gone[drop["id"]] = keep["id"]
                    if not dry_run:
                        keep["importance"] = max(keep.get("importance", 3), drop.get("importance", 3))
                        keep["uses"] = keep.get("uses", 0) + drop.get("uses", 0)
        report["merged"] = sorted(gone.items())
        report["flagged"] = [(x, y) for x, y in flagged_pairs if x not in gone and y not in gone]

        # decay + stale archive (merged/dropped ones are skipped)
        archive_stale: List[int] = []
        for d in docs:
            if d["id"] in gone or d.get("category") in PINNED_CATEGORIES or d.get("importance", 3) >= 5:
                continue
            age = _age_days(d, now)
            if age is None:
                continue
            if d.get("importance", 3) <= 1:
                if age >= STALE_DAYS:
                    archive_stale.append(d["id"])
                continue
            since = _age_days({"last_used": d.get("decayed_at")}, now)
            if age >= DECAY_DAYS and (since is None or since >= DECAY_DAYS):
                report["decayed"].append(d["id"])
                if not dry_run:
                    d["importance"] = max(1, d["importance"] - 1)
                    d["decayed_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        report["archived"] = archive_stale

        if not dry_run:
            flags: Dict[int, str] = {}
            for x, y in report["flagged"]:
                flags.setdefault(x, f"may conflict with #{y}")
                flags.setdefault(y, f"may conflict with #{x}")
            conductor.set_flags(flags)                              # also saves the merged importance / decay changes
            for drop_id, keep_id in gone.items():
                conductor.archive(drop_id, merged_into=keep_id)
            for mid in archive_stale:
                conductor.archive(mid)
    return report


def summarize(report: dict, conductor=None) -> List[str]:
    """Human-readable lines for the daily note."""
    lines = []
    text = (lambda i: next((d["text"] for d in (conductor.all() if conductor else []) if d["id"] == i), "")) if conductor else (lambda i: "")
    for drop, keep in report.get("merged", []):
        lines.append(f"merged duplicate #{drop} into #{keep}")
    for x, y in report.get("flagged", []):
        lines.append(f"possible conflict: #{x} \"{text(x)[:60]}\" vs #{y} \"{text(y)[:60]}\"")
    if report.get("decayed"):
        lines.append(f"lowered importance of {len(report['decayed'])} memories nobody used for {DECAY_DAYS}+ days")
    if report.get("archived"):
        lines.append(f"archived {len(report['archived'])} stale memories (kept in the database)")
    return lines
