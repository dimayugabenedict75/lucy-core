# Lucy Core - Proactive system

Install (from your Lucy_Core folder, after copying these files over it):

    python tools/install_proactive.py        # idempotent; backs up to runtime/backups/proactive_install/
    pip install pyyaml                       # already present if skills load
Restart Lucy Core. Then:

    1. Set `timezone:` in .core/proactive.yaml (Settings says UTC; quiet hours use this).
    2. Open the chat, then:  POST /api/proactive/test   (or ask Lucy: "remind me in 2 minutes to stretch")
    3. Run tests:  python -m pytest tests -q

Files: src/lucy/proactive/*, .core/proactive.yaml, tools/install_proactive.py, tests/test_proactive*.py.
The installer edits api.py, agent_loop.py, multistep.py, harness_info.py, .core/harness.md, .core/personas/lucy.md
(CRLF preserved). Rollback: copy the .bak files back.
