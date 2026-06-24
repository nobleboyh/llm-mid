"""Centralized CLI for eval utilities.

Usage:
    python -m eval.cli score [--n 50] [--category ...] [--prompt-id ...]
    python -m eval.cli headroom [--days 14]
    python -m eval.cli clear-redis [--hard]
    python -m eval.cli --help
"""

from __future__ import annotations

import sys

HELP = __doc__

COMMANDS = {
    "score": "eval.score_view_interactive",
    "headroom": "eval.headroom_view_interactive",
    "clear-redis": "eval.clear_redis",
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(HELP)
        return

    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(HELP, file=sys.stderr)
        sys.exit(1)

    # Remove the command name so the sub-module's own argparser sees only its own args
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if cmd == "score":
        from eval.score_view_interactive import main as fn
    elif cmd == "headroom":
        from eval.headroom_view_interactive import main as fn
    else:  # clear-redis
        from eval.clear_redis import main as fn

    fn()


if __name__ == "__main__":
    main()
