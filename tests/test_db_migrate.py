"""Versioned migrations: run once in order, back up first, roll back a failing one, daily backup once per day."""
import pathlib
import sqlite3
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from lucy.server import db_migrate as m  # noqa: E402


def mk(tmp_path, rows=True):
    p = tmp_path / "s.sqlite"
    c = sqlite3.connect(p, check_same_thread=False)
    c.execute("CREATE TABLE messages (a, created_at)")
    if rows:
        c.execute("INSERT INTO messages VALUES (1, 'x')")
    c.commit()
    return p, c


def test_applies_pending_in_order_and_is_idempotent(tmp_path):
    p, c = mk(tmp_path)
    r = m.run(c, threading.RLock(), p, dest_dir=tmp_path / "b")
    assert r["from"] == 0 and r["to"] == m.MIGRATIONS[-1][0] and r["error"] is None
    assert m.version(c) == r["to"]
    assert c.execute("SELECT name FROM sqlite_master WHERE name='idx_messages_created'").fetchone()
    again = m.run(c, threading.RLock(), p, dest_dir=tmp_path / "b")
    assert again["applied"] == []


def test_backup_made_when_there_is_data_not_for_an_empty_db(tmp_path):
    p, c = mk(tmp_path, rows=True)
    r = m.run(c, threading.RLock(), p, dest_dir=tmp_path / "b")
    assert r["backup"] and pathlib.Path(r["backup"]).is_file()
    b = sqlite3.connect(r["backup"])
    assert b.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1          # backup holds the pre-migration data
    b.close()
    p2 = tmp_path / "e"; p2.mkdir()
    p3, c3 = mk(p2, rows=False)
    assert m.run(c3, threading.RLock(), p3, dest_dir=p2 / "b")["backup"] is None


def test_failing_migration_rolls_back_and_keeps_version(tmp_path):
    p, c = mk(tmp_path)
    m.run(c, threading.RLock(), p, dest_dir=tmp_path / "b")
    good = m.version(c)

    def bad(cn):
        cn.execute("CREATE TABLE half_done (a)")
        raise RuntimeError("boom")
    r = m.run(c, threading.RLock(), p, migrations=[(good + 1, "bad", bad)], dest_dir=tmp_path / "b")
    assert "boom" in r["error"] and m.version(c) == good
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='half_done'").fetchone()


def test_daily_backup_once_per_day_and_pruned(tmp_path):
    p, c = mk(tmp_path)
    c.close()
    d = tmp_path / "b"
    assert m.daily_backup(p, dest_dir=d) is not None
    assert m.daily_backup(p, dest_dir=d) is None                  # same day: no second copy
    for i in range(10):                                           # older days pile up -> only `keep` are kept
        (d / f"s.daily.2020010{i % 10}0000{i:02d}.sqlite").write_bytes(b"x")
    m.backup(p, "daily", keep=3, dest_dir=d)
    assert len(list(d.glob("s.daily*.sqlite"))) <= 3
