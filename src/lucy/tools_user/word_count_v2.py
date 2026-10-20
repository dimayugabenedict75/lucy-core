"""User tool `word_count_v2` (written by Lucy with user_tool, version 1). Do not edit by hand: change it with
user_tool action=improve, otherwise the harness disables it (its hash is checked before every run).
Reason: Initial word_count tool had a creation conflict, so I'm creating word_count_v2.
"""
# Informational only: the real tier is the user's choice (Settings > Evolve), stored outside this file.
SCHEMA = {'type': 'function', 'function': {'name': 'word_count_v2', 'description': 'Counts the total number of words in a given string.', 'parameters': {'type': 'object', 'properties': {'text': {'type': 'string', 'description': 'The text to count.'}}, 'required': []}}}
TIER = 'exec'
LABEL = "run"
NEEDS = []


def handler(args, ctx=None):
    return run(args)


# ---- tool code ------------------------------------------------------------------------------------------------
def run(args):
    text = args.get('text', '')
    words = text.split()
    return str(len(words))
