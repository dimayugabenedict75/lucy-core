"""Tool registry unit tests (no server, no GPU). Run: pytest tests/test_tool_registry.py"""
import asyncio, pathlib, sys, tempfile, textwrap, threading, types, uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pytest  # noqa: E402
from lucy.tools import registry as R  # noqa: E402


def _schema(name="demo_tool", desc="Does a demo thing."):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}}}


def _make_pkg(files: dict) -> str:
    """Create a throw-away package on sys.path with the given module sources; returns its import name."""
    root = pathlib.Path(tempfile.mkdtemp())
    name = "fake_tools_" + uuid.uuid4().hex[:8]
    (root / name).mkdir()
    (root / name / "__init__.py").write_text("")
    for fname, src in files.items():
        (root / name / fname).write_text(textwrap.dedent(src))
    sys.path.insert(0, str(root))
    return name


GOOD = '''
    SCHEMA = {"type": "function", "function": {"name": "echo", "description": "Echo x.",
              "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}}}
    TIER = "read"
    LABEL = "read"
    def handler(args, ctx):
        return "echo:" + args.get("x", "")
'''


def test_real_tools_are_all_complete():
    """Every tool that ships in src/lucy/tools has a schema, handler, tier and label."""
    reg = R.Registry(); reg.discover("lucy.tools")
    for s in reg.all_specs():
        fn = s.schema["function"]
        assert fn["name"] == s.name and fn["description"].strip() and isinstance(fn["parameters"], dict)
        assert callable(s.handler) and s.tier in R.VALID_TIERS and s.label.strip(), s.name
        assert len(fn["description"]) <= 700, f"{s.name}: runaway description (trim it; small models read all of it)"


def test_discovery_loads_a_dropped_in_file_and_ignores_helpers():
    pkg = _make_pkg({"echo.py": GOOD, "helper.py": "def util():\n    return 1\n"})
    reg = R.Registry()
    assert reg.discover(pkg) == 1
    spec = reg.get("echo")
    assert spec and spec.tier == "read" and spec.label == "read"
    assert reg.discover(pkg) == 0                      # second call adds nothing, no duplicate error
    assert asyncio.run(reg.run("echo", {"x": "hi"})) == "echo:hi"


def test_family_module_with_TOOLS_list():
    pkg = _make_pkg({"fam.py": f'''
        def _s(n): return {{"type": "function", "function": {{"name": n, "description": "d",
                          "parameters": {{"type": "object", "properties": {{}}}}}}}}
        def a(args, ctx): return "A"
        async def b(args, ctx): return "B"
        TOOLS = [dict(schema=_s("tool_a"), handler=a, tier="read", label="read"),
                 dict(schema=_s("tool_b"), handler=b, tier="write", label="edit")]
    '''})
    reg = R.Registry(); assert reg.discover(pkg) == 2
    assert asyncio.run(reg.run("tool_a", {})) == "A" and asyncio.run(reg.run("tool_b", {})) == "B"


GOOD_D = textwrap.dedent(GOOD)


@pytest.mark.parametrize("breakage", [
    'TIER = "read"\nLABEL = "x"\ndef handler(a, c): return ""\n',                                   # no SCHEMA
    GOOD_D.replace('TIER = "read"', 'TIER = "godmode"'),                                            # bad tier
    GOOD_D.replace('LABEL = "read"', 'LABEL = ""'),                                                # empty label
    GOOD_D.replace("def handler(args, ctx):", "handler = 5\ndef _unused(args, ctx):"),            # handler not callable
    GOOD_D.replace('"description": "Echo x.",', ""),                                              # no description
    GOOD_D.replace('TIER = "read"\n', ""),                                                        # TIER missing
])
def test_incomplete_modules_fail_loudly(breakage):
    pkg = _make_pkg({"bad.py": breakage})
    with pytest.raises(R.ToolSpecError):
        R.Registry().discover(pkg)


def test_duplicate_tool_names_are_rejected():
    pkg = _make_pkg({"one.py": GOOD, "two.py": GOOD})
    with pytest.raises(R.ToolSpecError):
        R.Registry().discover(pkg)


def test_sync_handler_runs_off_the_event_loop_thread():
    seen = {}
    def slow(args, ctx):
        seen["thread"] = threading.current_thread()
        return 42                                      # non-str results are stringified
    reg = R.Registry()
    reg.register(R.ToolSpec("slow", _schema("slow"), slow, "read", "read", "test"))
    async def go():
        main = threading.current_thread()
        out = await reg.run("slow", {}, R.ToolContext(session_id="s1"))
        return out, main
    out, main = asyncio.run(go())
    assert out == "42" and seen["thread"] is not main


def test_handler_gets_context():
    reg = R.Registry()
    reg.register(R.ToolSpec("who", _schema("who"), lambda a, c: f"session={c.session_id}", "read", "read", "test"))
    assert asyncio.run(reg.run("who", {}, R.ToolContext(session_id="abc"))) == "session=abc"


def test_toolset_import_still_works():
    from lucy.tools import Toolset
    assert hasattr(Toolset, "list_drive_files")


def test_google_tools_are_registered_and_call_toolset():
    from lucy.tools import google_drive
    reg = R.Registry(); reg.discover("lucy.tools")
    for name, label in (("list_drive_files", "drive"), ("create_google_doc", "docs"), ("get_doc_content", "docs")):
        s = reg.get(name)
        assert s and s.tier == "network" and s.label == label, name
    T = google_drive.Toolset
    saved = (T.list_drive_files, T.create_google_doc, T.get_doc_content)
    T.list_drive_files = lambda self: "files!"
    T.create_google_doc = lambda self, title: f"created:{title}"
    T.get_doc_content = lambda self, title: f"content:{title}"
    try:
        assert asyncio.run(reg.run("list_drive_files", {})) == "files!"
        assert asyncio.run(reg.run("create_google_doc", {"title": "Plan"})) == "created:Plan"
        assert asyncio.run(reg.run("get_doc_content", {"title": "Plan"})) == "content:Plan"
        assert asyncio.run(reg.run("get_doc_content", {})) == "content:"      # missing arg behaves like before
    finally:
        T.list_drive_files, T.create_google_doc, T.get_doc_content = saved


def test_file_tools_work_through_the_registry():
    import tempfile
    from lucy import paths as _paths
    from lucy.tools import _common
    reg = R.Registry(); reg.discover("lucy.tools")
    for name, tier in (("read_local_file", "read"), ("edit_local_file", "write"), ("write_local_file", "write"),
                       ("list_local_files", "read"), ("find_file", "read"), ("send_file", "read")):
        assert reg.get(name) and reg.get(name).tier == tier, name
    tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
    saved = (_paths.SAFE_ROOT, _paths.ROOT)
    _paths.SAFE_ROOT = _paths.ROOT = tmp
    try:
        f = (tmp / "a.txt").as_posix()
        run = lambda n, a: asyncio.run(reg.run(n, a))
        assert "Error" not in run("write_local_file", {"file_path": f, "content": "hello\nworld\n"})
        assert "hello" in run("read_local_file", {"file_path": f})
        assert "2: world" in run("read_local_file", {"file_path": f, "search": "world"})
        run("edit_local_file", {"file_path": f, "old_string": "world", "new_string": "there"})
        assert "there" in run("read_local_file", {"file_path": f})
        assert "a.txt" in run("list_local_files", {"dir_path": tmp.as_posix()})
        assert run("send_file", {"file_path": f, "caption": "hi"}) == "MEDIA:a.txt hi"
        assert run("read_local_file", {"file_path": ""}) == _common.outside_root("")     # unresolvable path -> refusal message
        assert run("write_local_file", {"file_path": "", "content": "x"}) == _common.outside_root("")
    finally:
        _paths.SAFE_ROOT, _paths.ROOT = saved


def test_web_and_media_tools_are_registered_with_tiers_and_labels():
    reg = R.Registry(); reg.discover("lucy.tools")
    want = {"web_search": ("network", "search"), "web_extract": ("network", "read"), "analyze_image": ("read", "analyze"),
            "send_tts": ("exec", "speak"), "send_sticker": ("read", "send_sticker")}
    for name, (tier, label) in want.items():
        s = reg.get(name)
        assert s and (s.tier, s.label) == (tier, label), name


def test_web_tools_call_their_helpers():
    from lucy.tools import web
    reg = R.Registry(); reg.discover("lucy.tools")
    saved = (web._web_search, web._web_extract)
    async def fake_search(q, limit): return f"S:{q}:{limit}"
    async def fake_extract(urls, limit): return f"E:{urls}:{limit}"
    web._web_search, web._web_extract = fake_search, fake_extract
    try:
        run = lambda n, a: asyncio.run(reg.run(n, a))
        assert run("web_search", {"query": "cats"}) == "S:cats:5"
        assert run("web_search", {"query": "cats", "limit": 3}) == "S:cats:3"
        assert run("web_extract", {"urls": ["u"]}) == "E:['u']:15000"
    finally:
        web._web_search, web._web_extract = saved


def test_media_tools_basic_paths():
    import tempfile
    from lucy import paths as _paths
    from lucy.tools import media
    reg = R.Registry(); reg.discover("lucy.tools")
    run = lambda n, a, c=None: asyncio.run(reg.run(n, a, c))
    assert run("analyze_image", {"image_path": ""}).startswith("Image not found or outside allowed path")
    assert run("send_tts", {"text": ""}) == "send_tts requires 'text' parameter"
    seen = {}
    saved_clean = media.clean_for_speech
    media.clean_for_speech = lambda text, names: (seen.setdefault("names", names), "")[1]
    try:
        out = run("send_tts", {"text": "hello"}, R.ToolContext(tool_names=("a", "b")))
    finally:
        media.clean_for_speech = saved_clean
    assert out.startswith("send_tts: nothing speakable") and seen["names"] == ("a", "b")
    # stickers: real files in a temp "Lucy Core"
    tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
    for theme in ("Dark", "Light"):
        d = tmp / "assets" / "stickers" / theme; d.mkdir(parents=True)
        (d / "Happy.png").write_bytes(b"\x89PNG")
    saved = (_paths.SAFE_ROOT, _paths.ROOT)
    _paths.SAFE_ROOT = _paths.ROOT = tmp
    try:
        out = run("send_sticker", {"emotion": "happy", "caption": "hi"})
        assert out == "MEDIA:assets/stickers/Dark/Happy.png hi", out
        assert not run("send_sticker", {"emotion": "no-such-feeling"}).startswith("MEDIA:")
    finally:
        _paths.SAFE_ROOT, _paths.ROOT = saved


def test_no_tool_module_defines_the_same_top_level_name_twice():
    """A handler named like its helper silently shadows it (and then calls itself). Catch that class of bug."""
    import ast
    folder = pathlib.Path(R.__file__).parent
    for f in folder.glob("*.py"):
        seen = {}
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            names = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [(a.asname or a.name).split(".")[0] for a in node.names]
            for n in names:
                seen[n] = seen.get(n, 0) + 1
        assert not {k: v for k, v in seen.items() if v > 1}, f"{f.name}: duplicate top-level names {seen}"


def test_shell_and_cua_tools_are_registered():
    reg = R.Registry(); reg.discover("lucy.tools")
    assert (reg.get("run_shell_command").tier, reg.get("run_shell_command").label) == ("exec", "run")
    for n in ("cua_cursor", "cua_mouse", "cua_keyboard"):
        assert (reg.get(n).tier, reg.get(n).label) == ("system", "control"), n


def test_cua_tools_dispatch_to_the_cua_module():
    import json
    calls = []
    fake = types.ModuleType("lucy.cua.lucy_cua")
    fake.get_cursor_pos = lambda: {"x": 1, "y": 2}
    fake.set_cursor_pos = lambda x, y: {"moved": [x, y]}
    fake.mouse_click = lambda x, y, b: {"clicked": [x, y, b]}
    fake.mouse_drag = lambda *a: {"drag": list(a)}
    fake.key_combo = lambda keys: calls.append(keys) or {"keys": keys}
    fake.type_text = lambda t: {"typed": t}
    fake.minimize_all = lambda: {}
    saved = sys.modules.get("lucy.cua.lucy_cua")
    sys.modules["lucy.cua.lucy_cua"] = fake
    try:
        reg = R.Registry(); reg.discover("lucy.tools")
        run = lambda n, a: json.loads(asyncio.run(reg.run(n, a)))
        assert run("cua_cursor", {}) == {"x": 1, "y": 2}
        assert run("cua_cursor", {"action": "move", "x": 5, "y": 6}) == {"moved": [5, 6]}
        assert run("cua_mouse", {"x": 1, "y": 2}) == {"clicked": [1, 2, "left"]}
        assert run("cua_keyboard", {"action": "minimize"}) == {"keys": ["win", "d"]} and calls == [["win", "d"]]
        assert "Unknown keyboard action" in run("cua_keyboard", {"action": "dance"})["error"]
        assert "error" in run("cua_mouse", {"action": "click"})                   # missing x/y -> error JSON, not a crash
    finally:
        if saved is None: sys.modules.pop("lucy.cua.lucy_cua", None)
        else: sys.modules["lucy.cua.lucy_cua"] = saved
