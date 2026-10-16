"""Google Drive / Docs tools (moved out of api.py unchanged; the work is done by google_drive.Toolset)."""


def _toolset():
    from .google_drive import Toolset
    return Toolset()


def _list_drive_files(args, ctx):
    return _toolset().list_drive_files()


def _create_google_doc(args, ctx):
    return _toolset().create_google_doc(args.get("title", ""))


def _get_doc_content(args, ctx):
    return _toolset().get_doc_content(args.get("title", ""))


_TITLE = lambda text: {"type": "object", "properties": {"title": {"type": "string", "description": text}},
                       "required": ["title"]}

TOOLS = [
    {
        "schema": {"type": "function", "function": {
            "name": "list_drive_files",
            "description": "List the most recent files in your Google Drive.",
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        "handler": _list_drive_files, "tier": "network", "label": "drive",
    },
    {
        "schema": {"type": "function", "function": {
            "name": "create_google_doc",
            "description": "Create a new Google Doc with the given title and return its name and ID.",
            "parameters": _TITLE("The title for the new Google Doc")}},
        "handler": _create_google_doc, "tier": "network", "label": "docs",
    },
    {
        "schema": {"type": "function", "function": {
            "name": "get_doc_content",
            "description": "Retrieve the full text content of a Google Doc by its title.",
            "parameters": _TITLE("The title of the Google Doc to retrieve")}},
        "handler": _get_doc_content, "tier": "network", "label": "docs",
    },
]
