"""User tool `version_check` (written by Lucy with user_tool, version 1). Do not edit by hand: change it with
user_tool action=improve, otherwise the harness disables it (its hash is checked before every run).
Reason: To verify basic module accessibility.
"""
# Informational only: the real tier is the user's choice (Settings > Evolve), stored outside this file.
SCHEMA = {'type': 'function', 'function': {'name': 'version_check', 'description': 'Checks the Python version.', 'parameters': {'type': 'object', 'properties': {}, 'required': []}}}
TIER = 'exec'
LABEL = "run"
NEEDS = []


def handler(args, ctx=None):
    return run(args)


# ---- tool code ------------------------------------------------------------------------------------------------
def run(args):
    import sys
    return f"Python version: {sys.version}"
