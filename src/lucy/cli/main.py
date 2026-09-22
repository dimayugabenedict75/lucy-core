"""Lucy Core CLI — coding-focused dev shell.

Separate from the daily companion (GUI/chat). Uses technical tone (temp ~0.3).
Streams responses token-by-token, with tool-use events interleaved.

Usage:
    python -m lucy.cli.main [message]
    python -m lucy.cli.main —interactive  # stdin REPL loop
    lucy <message>                         # if installed via pyproject entry-point

Environment:
    LUCY_CORE_URL  — API base URL (default: http://localhost:8090)
    LUCY_API_KEY   — API key for auth (default: dev-harness)
"""

import sys
import os
import json
import argparse
import httpx
from typing import Optional

API_BASE = os.environ.get("LUCY_CORE_URL", "http://localhost:8090").rstrip("/")
API_KEY = os.environ.get("LUCY_API_KEY", "dev-harness")
DEFAULT_SESSION = "cli-dev"

EMOJI = {
    "activity": "[lucy-core]",
    "tool": "[tool]",
    "error": "[error]",
    "done": "[ok]",
}


def _print_activity(msg: str):
    """Print a status/activity line to stderr so it doesn't pollute stdout."""
    print(f"{EMOJI['activity']} {msg}", file=sys.stderr, flush=True)


def _print_tool_use(name: str, args: dict):
    """Print a tool-use event to stderr."""
    print(f"{EMOJI['tool']} {name}({json.dumps(args, ensure_ascii=False)[:100]})", file=sys.stderr, flush=True)


def _print_error(msg: str):
    print(f"{EMOJI['error']} {msg}", file=sys.stderr, flush=True)


def stream_chat(
    message: Optional[str] = None,
    session_id: str = DEFAULT_SESSION,
    interactive: bool = False,
) -> None:
    """Stream a single chat message to Lucy Core and print token-by-token."""
    if not message and not interactive:
        message = ""

    if interactive:
        print(f"Lucy Core CLI — coding mode (temp=0.3). Type /quit or Ctrl+C to exit.", file=sys.stderr)
        while True:
            try:
                user_input = input("λ ")
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.", file=sys.stderr)
                break
            if not user_input.strip():
                continue
            if user_input.strip() in ("/quit", "/exit"):
                break
            _send_message(user_input, session_id)
        return

    _send_message(message, session_id)


def _send_message(message: str, session_id: str) -> None:
    """Send a single message to /api/cli and stream the response."""
    params = {"session_id": session_id, "api_key": API_KEY}
    payload = {
        "message": message,
        "stream": True,
        "api_key": API_KEY,
        "session_id": session_id,
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            with client.stream("POST", f"{API_BASE}/api/cli", params=params, json=payload) as response:
                if response.status_code != 200:
                    _print_error(f"HTTP {response.status_code}: {response.text}")
                    return

                buffer = ""
                for chunk in response.iter_text():
                    if chunk.startswith("data: "):
                        raw = chunk[6:].strip()
                        if raw == "[DONE]":
                            break
                        if raw.startswith("[METRICS]"):
                            metrics = json.loads(raw[9:])
                            _print_activity(
                                f"Generated {metrics['tokens_generated']} tokens in "
                                f"{metrics['time_ms']}ms ({metrics['tokens_per_second']} T/s, "
                                f"{metrics['tool_calls']} tool calls)"
                            )
                            print(f"\n{EMOJI['done']}", file=sys.stderr, flush=True)
                            break
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if "content" in data:
                            # Print content chunk to stdout, no buffering
                            sys.stdout.write(data["content"])
                            sys.stdout.flush()
                        elif "activity" in data and data["activity"]:
                            _print_activity(data["activity"])
                        elif "media" in data and data["media"]:
                            # File attachment — print path on stderr
                            print(f"{EMOJI['tool']} Media: {data['media']}", file=sys.stderr, flush=True)
                    else:
                        # Plain SSE fragment
                        buffer += chunk
                        sys.stdout.flush()
    except httpx.ConnectError:
        _print_error(f"Cannot connect to Lucy Core at {API_BASE}. Is the server running?")
        sys.exit(1)
    except Exception as e:
        _print_error(str(e))
        sys.exit(1)

    # Newline after streamed content
    print()


def main():
    """Entry point for the `lucy` CLI command."""
    parser = argparse.ArgumentParser(
        prog="lucy",
        description="Lucy Core CLI — coding-focused agent shell.",
    )
    parser.add_argument("message", nargs="?", default=None, help="Message to send (or use —interactive)")
    parser.add_argument("--interactive", "-i", action="store_true", help="Start REPL loop")
    parser.add_argument("--session", "-s", default=DEFAULT_SESSION, help="Session ID (default: cli-dev)")
    args = parser.parse_args()

    if args.interactive:
        stream_chat(interactive=True, session_id=args.session)
    else:
        if not args.message:
            parser.print_help()
            sys.exit(1)
        stream_chat(message=args.message, session_id=args.session)


if __name__ == "__main__":
    main()