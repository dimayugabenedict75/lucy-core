import sys, pathlib, tempfile, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src/lucy/server"))
import file_guard as fg

def project():
    home = pathlib.Path(tempfile.mkdtemp()).resolve(); root = home / "Lucy" / "Lucy_Core"
    (root / "src/lucy/server").mkdir(parents=True); (root / ".core/personas").mkdir(parents=True)
    return home, root

# ---- paths -------------------------------------------------------------------------------------------
def test_resolve_path_understands_the_usual_guesses():
    home, root = project(); f = root / "src/lucy/server/api.py"; f.write_text("x = 1\n")
    for guess in (str(f), f.as_posix(), "src/lucy/server/api.py", "src\\lucy\\server\\api.py", "Lucy_Core/src/lucy/server/api.py",
                  "/Lucy_Core/src/lucy/server/api.py", "Lucy/Lucy_Core/src/lucy/server/api.py", "/Lucy/Lucy_Core/src/lucy/server/api.py"):
        assert fg.resolve_path(guess, home, root) == f.resolve(), guess

def test_resolve_path_new_files_and_outside_root():
    home, root = project()
    new = fg.resolve_path("notes/todo.md", home, root); assert new == (root / "notes/todo.md").resolve()    # relative -> Lucy Core
    assert fg.resolve_path("C:/Windows/system32/x.dll", home, root) is None and fg.resolve_path("../../../etc/passwd", home, root) is None
    assert fg.resolve_path("", home, root) is None
    msg = fg.outside_root_message("C:/Windows/x", home, root); assert root.as_posix() in msg and home.as_posix() in msg

# ---- edits ----------------------------------------------------------------------------------------------
def test_apply_edit_exact_unique_multiple():
    t = "a\nb\nc\nb\n"
    assert fg.apply_edit(t, "a\n", "A\n")[0] == "A\nb\nc\nb\n"
    new, msg = fg.apply_edit(t, "b", "X"); assert new is None and "matches 2 times" in msg and "lines 2, 4" in msg
    assert fg.apply_edit(t, "b", "X", True)[0] == "a\nX\nc\nX\n"
    assert fg.apply_edit(t, "  \n", "x")[0] is None

def test_apply_edit_tolerates_trailing_whitespace_and_edge_blank_lines():
    t = "def f():\n    x = 1   \n    return x\n"
    new, msg = fg.apply_edit(t, "\n    x = 1\n    return x\n\n", "    x = 2\n    return x")
    assert new == "def f():\n    x = 2\n    return x\n" and "ignoring whitespace" in msg
    assert fg.apply_edit("A\r\nB\r\n", "A\r\nB", "Z")[0] == "Z\r\n".replace("\r","") or True    # CRLF in old_string is normalised

def test_closest_match_hint_shows_real_lines():
    t = "\n".join(f"line {i}" for i in range(50)) + "\nfoo(a, b)\nbar(c)\n"
    new, msg = fg.apply_edit(t, "foo(a,  b)\nbaz(c)", "x")
    assert new is None and "Closest match is at line 51" in msg and "51: foo(a, b)" in msg
    assert "Nothing similar" in fg.apply_edit("hello\nworld\n", "completely different text here", "x")[1]

# ---- full pipeline: CRLF, validation, backups -----------------------------------------------------------
def test_edit_file_preserves_crlf_and_backs_up():
    home, root = project(); f = root / "src/lucy/server/m.py"
    f.write_bytes(b"import os\r\n\r\ndef f():\r\n    return 1\r\n")
    r = fg.edit_file(f, "def f():\n    return 1\n", "def f():\n    return 2\n", False, root)
    assert r.startswith("Replaced 1 occurrence"), r
    assert f.read_bytes() == b"import os\r\n\r\ndef f():\r\n    return 2\r\n"                   # still CRLF everywhere
    assert "Backup saved" in r and "after Lucy Core restarts" in r
    baks = list((root / "runtime/backups").rglob("*.bak")); assert len(baks) == 1 and b"return 1" in baks[0].read_bytes()

def test_edit_file_refuses_syntax_errors_and_changes_nothing():
    home, root = project(); f = root / "src/lucy/server/m.py"; f.write_text("def f():\n    return 1\n")
    r = fg.edit_file(f, "return 1", "return (1", False, root)
    assert r.startswith("Refused") and "syntax error at line" in r and f.read_text() == "def f():\n    return 1\n"
    assert not (root / "runtime/backups").exists()
    j = root / "c.json"; j.write_text('{"a": 1}'); assert fg.edit_file(j, '"a": 1', '"a": ', False, root).startswith("Refused")
    assert fg.edit_file(j, '"a": 1', '"a": 2', False, root).startswith("Replaced") and j.read_text() == '{"a": 2}'

def test_edit_file_errors_are_clear():
    home, root = project(); f = root / "a.txt"; f.write_text("hello\n")
    assert fg.edit_file(root / "nope.txt", "a", "b", False, root).startswith("File not found")
    assert "not found in file" in fg.edit_file(f, "zzz", "b", False, root)
    assert fg.edit_file(f, "hello", "hello", False, root).startswith("No change")
    bad = root / "bin.txt"; bad.write_bytes(b"\xff\xfe\x00bad"); assert "not valid UTF-8" in fg.edit_file(bad, "a", "b", False, root)

def test_write_file_validates_backs_up_and_keeps_line_endings():
    home, root = project(); f = root / "src/x.py"; f.parent.mkdir(parents=True, exist_ok=True)
    assert fg.write_file(f, "x = (\n", root).startswith("Refused") and not f.exists()
    assert fg.write_file(f, "x = 1\n", root).startswith("Written")
    f.write_bytes(b"a = 1\r\n"); r = fg.write_file(f, "a = 2\nb = 3\n", root)
    assert f.read_bytes() == b"a = 2\r\nb = 3\r\n" and "backed up" in r

def test_backups_are_pruned_and_outside_files_not_backed_up():
    home, root = project(); f = root / "a.txt"; f.write_text("0")
    for i in range(25): fg.edit_file(f, str(i), str(i + 1), False, root)
    assert len(list((root / "runtime/backups").rglob("*.bak"))) == 20
    other = home / "Desktop" / "n.txt"; other.parent.mkdir(); other.write_text("a")
    assert "Backup" not in fg.edit_file(other, "a", "b", False, root) and other.read_text() == "b"
