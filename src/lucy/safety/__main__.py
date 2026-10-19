"""Command line for the USER (run it yourself in a terminal; Lucy's shell tool cannot answer the prompt):

    python -m lucy.safety status     show mode, pause flag and whether the safety files match the lock
    python -m lucy.safety seal       after YOU changed permissions.yaml or the safety code: accept the change
    python -m lucy.safety backups    list recent undo snapshots
    python -m lucy.safety restore ID put a file back (any chat's change)
"""
from __future__ import annotations

import sys

from . import get


def main(argv) -> int:
    layer = get()
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        c = layer.constitution
        diff = c.diff() if c.lock.exists() else ["not sealed yet (first start of Lucy Core seals it)"]
        print(f"mode: {layer.mode}   paused: {layer.paused}")
        print("safety files: " + ("OK, match the lock" if not diff else "CHANGED: " + "; ".join(diff)))
        for p in layer.cfg.problems:
            print("config problem:", p)
        return 0
    if cmd == "seal":
        c = layer.constitution
        diff = c.diff() if c.lock.exists() else ["(no lock yet)"]
        if not diff:
            print("Nothing to seal: the safety files already match the lock.")
            return 0
        print("These safety files differ from the lock:\n  " + "\n  ".join(diff))
        if not sys.stdin.isatty():
            print("Refusing: seal must be confirmed interactively by a person at a terminal.")
            return 2
        if input("Did YOU make these changes and want to keep them? Type SEAL to accept: ").strip() != "SEAL":
            print("Not sealed.")
            return 1
        c.seal(by="user (cli)")
        layer.audit.write(event="seal", by="user (cli)", files=diff)
        print("Sealed. Restart Lucy Core.")
        return 0
    if cmd == "backups":
        for e in layer.backups.list(limit=40):
            print(f"{e['id']}  {e['ts']}  {e['kind']:9}  {e['tool']:18}  {e['path']}")
        return 0
    if cmd == "restore" and len(argv) > 1:
        print(layer.backups.restore(argv[1], session=None, by="user (cli)"))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
