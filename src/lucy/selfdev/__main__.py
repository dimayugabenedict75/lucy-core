"""`python -m lucy.selfdev rollback [RELEASE]` | `releases` | `status`  (rollback.bat calls this).

Stdlib + lane_b only: it must work when Lucy Core itself no longer starts after a change.
"""
import sys
from pathlib import Path

from . import lane_b, state_dir, workspaces_dir, _root


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "releases"
    lb = lane_b.LaneB(root=_root(), state_dir=state_dir(), workspaces=workspaces_dir())
    if cmd == "releases" or cmd == "status":
        rels = lb.releases()
        if not rels:
            print("No Lane B changes were ever applied.")
        for m in rels:
            n = sum(len(v) for v in m["changes"].values())
            print(f"{m['id']}  {m.get('status'):12s}  {n} files  {m.get('title', '')}")
        return 0
    if cmd == "rollback":
        rid = argv[1] if len(argv) > 1 else None
        try:
            m = lb.rollback(rid, by="user (rollback.bat)")
        except lane_b.LaneBError as e:
            print(f"Nothing rolled back: {e}")
            return 1
        print(f"Rolled back {m['id']} ({m.get('title', '')}). The previous files are back. Start Lucy again.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
