"""Shared setup for the safety tests: a SafetyLayer on temporary folders, installed as the process default."""
import pathlib, sys, tempfile

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lucy import paths as _paths, safety  # noqa: E402

YAML = """mode: {mode}
approval_timeout_s: 10
folders:
  read: ["{{SAFE_ROOT}}"]
  write: ["{{SAFE_ROOT}}/work"]
ask_paths: ["{{LUCY_ROOT}}/src"]
denied_paths: ["{{SAFE_ROOT}}/secrets"]
readonly_paths: ["{{SAFE_ROOT}}/system"]
untrusted:
  tools: [web_search, web_extract]
  paths: ["{{SAFE_ROOT}}/Downloads"]
"""


class Env:
    """tmp/ (= LUCY_ROOT and SAFE_ROOT) with work/, secrets/, system/, Downloads/, src/, .core/permissions.yaml."""

    def __init__(self, mode="ask_for_risky", monkeypatch=None, enforce=False):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="lucy_safety_")).resolve()
        for d in ("work", "secrets", "system", "Downloads", "src", ".core", "runtime"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.root / ".core" / "permissions.yaml").write_text(YAML.format(mode=mode), encoding="utf-8")
        self.mp = monkeypatch
        if monkeypatch is not None:
            monkeypatch.setattr(_paths, "SAFE_ROOT", self.root)
            monkeypatch.setattr(_paths, "ROOT", self.root)
        self.layer = safety.SafetyLayer(root=self.root, runtime=self.root / "runtime", safe_root=self.root,
                                        home=self.root, enforce_constitution=False)
        if enforce:
            self.layer.start()
        if monkeypatch is not None:
            monkeypatch.setattr(safety, "_default", self.layer)     # undone automatically, even if the test fails
        else:
            safety.set_default(self.layer)

    def p(self, rel):
        return (self.root / rel).as_posix()

    def close(self):
        safety.set_default(None)
