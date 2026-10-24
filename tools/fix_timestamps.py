r"""One-time repair: shifts OLD chat/session timestamps that were saved in UTC to this PC's local time.
New messages are already saved in local time, so only rows from before the fix need this.

Dry run (default, changes nothing):
    .\.venv\Scripts\python.exe tools\fix_timestamps.py --before "2026-10-24 12:00:00"
Apply (stop Lucy Core first; a .bak copy of the database is made):
    .\.venv\Scripts\python.exe tools\fix_timestamps.py --before "2026-10-24 12:00:00" --apply

--before : rows with a time earlier than this are treated as UTC. Use the moment you installed this fix
           (in your local time). Offset is taken from this PC's clock, or force it with --hours 8."""
import argparse, shutil, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from lucy.paths import ROOT


def find_db():
    p = ROOT / "runtime" / "sessions.sqlite"            # same file api.py uses
    if not p.is_file():
        sys.exit(f"chat database not found: {p}")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    hours = a.hours if a.hours is not None else time.localtime().tm_gmtoff / 3600
    mod = f"{hours:+g} hours"
    db = find_db()
    print("database:", db, "| shift:", mod)
    c = sqlite3.connect(db)
    plan = [("messages", "created_at"), ("sessions", "updated_at"), ("summaries", "created_at")]
    for t, col in plan:
        try:
            n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} < ?", (a.before,)).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        print(f"{t}.{col}: {n} rows would shift")
    if not a.apply:
        print("dry run; add --apply to change them"); return
    shutil.copy2(db, str(db) + ".pre-tzfix.bak")
    with c:
        for t, col in plan:
            try:
                c.execute(f"UPDATE {t} SET {col} = strftime('%Y-%m-%d %H:%M:%S', {col}, ?) WHERE {col} < ?",
                          (mod, a.before))
            except sqlite3.OperationalError:
                pass
    print("done; backup:", str(db) + ".pre-tzfix.bak")


if __name__ == "__main__":
    main()
