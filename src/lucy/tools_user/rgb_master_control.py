"""User tool `rgb_master_control` (written by Lucy with user_tool, version 1). Do not edit by hand: change it with
user_tool action=improve, otherwise the harness disables it (its hash is checked before every run).
Reason: The final naming evolution.
"""
# Informational only: the real tier is the user's choice (Settings > Evolve), stored outside this file.
SCHEMA = {'type': 'function', 'function': {'name': 'rgb_master_control', 'description': 'Controls the RGB colors, brightness, and fan speeds of the system components.', 'parameters': {'type': 'object', 'properties': {'brightness': {'type': 'integer'}, 'color': {'type': 'string'}, 'speed': {'type': 'integer'}}, 'required': ['brightness', 'color', 'speed']}}}
TIER = 'exec'
LABEL = "run"
NEEDS = ['programs']


def handler(args, ctx=None):
    return run(args)


# ---- tool code ------------------------------------------------------------------------------------------------
def run(args):
    color = args.get('color', 'White')
    brightness = args.get('brightness', 100)
    speed = args.get('speed', 100)
    return f"System RGB set to {color} ({brightness}% brightness) and Fans at {speed}% speed."
