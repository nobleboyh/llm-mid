"""Interactive score board — browse worst/best calls with arrow-key navigation.

Usage:
    python -m eval.score_view_interactive

    # Filter by category
    python -m eval.score_view_interactive --category fhir_query

    # Filter by prompt version
    python -m eval.score_view_interactive --prompt-id v2_system_prompt

    # Show more records per bucket
    python -m eval.score_view_interactive --n 50

Keys:
    ↑ / ↓         Navigate records
    Tab / ← / →   Toggle between WORST and BEST sections
    Enter / Space Show full record details (Esc / q to go back)
    q / Ctrl+C    Quit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import termios

os.environ.setdefault("FORCE_COLOR", "1")
os.environ.setdefault("COLORTERM", "truecolor")
os.environ.setdefault("TERM", "xterm-256color")

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from eval.redis_store import get_best_calls, get_worst_calls

log = logging.getLogger("eval.score_view_interactive")

# ── Labels ──────────────────────────────────────────────────────────────────

WORST_LABEL = "⚠  WORST SCORING"
BEST_LABEL = "✓  BEST SCORING"


# ── Row formatting ──────────────────────────────────────────────────────────

def _format_row(i: int, active: bool, c: dict) -> Text:
    """Return a single row as a Rich Text, highlighted if active."""
    score = float(c.get("composite_score", 0))
    cat = str(c.get("request_category", "general"))
    model = str(c.get("model", ""))
    q = str(c.get("question", ""))

    style = "reverse" if active else ""
    s = Text()
    if active:
        s.append("▶ ", style="bold cyan")
    else:
        s.append("  ")
    s.append(f"{score:>6.3f}  ", style=f"bold yellow {style}")
    s.append(f"{cat:<14}", style=style)
    s.append(f"{model:<18}" if len(model) <= 18 else f"{model[:17]}…", style=style)
    s.append(
        f"{q[:40]}" if len(q) <= 40 else f"{q[:39]}…",
        style=style,
    )

    # Skill injection indicator
    sk = c.get("skill_name", "")
    if sk:
        s.append(f"  {sk:<20}", style=f"bold magenta {style}")
    return s


def _detail_view(record: dict, header: str) -> Panel:
    """Build a Rich Panel with full record details."""
    r = record
    score = float(r.get("composite_score", 0))
    scores_raw = r.get("scores", r.get("scores_json", {}))
    if isinstance(scores_raw, str):
        scores_raw = json.loads(scores_raw)

    lines: list[Text] = []
    lines.append(Text(header, style="bold white"))
    lines.append(Text(""))
    lines.append(Text.assemble(("Call ID:   ", "bold"), r.get("call_id", "-")))
    lines.append(
        Text.assemble(("Score:     ", "bold"), (f"{score:.4f}", "yellow"))
    )
    lines.append(
        Text.assemble(("Category:  ", "bold"), r.get("request_category", "-"))
    )
    lines.append(Text.assemble(("Model:     ", "bold"), r.get("model", "-")))
    lines.append(
        Text.assemble(("Prompt ID: ", "bold"), r.get("prompt_id", "-"))
    )
    lines.append(
        Text.assemble(
            ("Tokens:    ", "bold"),
            f"{r.get('tokens_in', '?')} in / {r.get('tokens_out', '?')} out",
        )
    )
    lines.append(
        Text.assemble(("Timestamp: ", "bold"), r.get("timestamp", "-"))
    )
    # ── Skill injection info (when present) ──────────────────────────
    skill_name = r.get("skill_name", "")
    if skill_name:
        lines.append(Text(""))
        lines.append(Text("Skill Injection:", style="bold underline"))
        lines.append(
            Text.assemble(
                ("  Skill(s):   ", "dim"),
                (skill_name, "bold magenta"),
            )
        )
    lines.append(Text(""))
    lines.append(Text("Per-dimension scores:", style="bold underline"))
    if scores_raw:
        for key, val in scores_raw.items():
            val_str = f"{val:.4f}" if isinstance(val, float) else str(val)
            lines.append(Text(f"  {key}:  {val_str}"))
    else:
        lines.append(Text("  (no per-dimension scores)"))
    lines.append(Text(""))
    lines.append(Text("Question:", style="bold underline"))
    lines.append(Text(f"  {r.get('question', '-')}"))
    lines.append(Text(""))
    lines.append(Text("Answer:", style="bold underline"))
    for ans_line in str(r.get("answer", "-")).split("\n"):
        lines.append(Text(f"  {ans_line}"))
    lines.append(Text(""))
    lines.append(Text("  Esc / q → back    Ctrl+C → quit", style="dim"))

    body = Text("\n").join(lines)
    return Panel(body, border_style="cyan")



# ── Raw terminal input ──────────────────────────────────────────────────────
# Assumes terminal is already in cbreak/raw mode.

def _getch() -> str:
    """Read a single keypress from stdin (expects terminal in raw mode).

    Returns:
        "up", "down", "left", "right", "tab", "enter", "space",
        "esc", "ctrl_c", "q", or raw single character.
    """
    fd = sys.stdin.fileno()
    import select

    b = os.read(fd, 1)
    if not b:
        return ""

    if b == b"\x1b":
        # ESC — read any following bytes with a short timeout
        rest = b""
        for _ in range(5):
            r, _, _ = select.select([fd], [], [], 0.01)
            if not r:
                break
            more = os.read(fd, 1)
            if not more:
                break
            rest += more
        seq = b"\x1b" + rest

        if seq == b"\x1b[A":
            return "up"
        if seq == b"\x1b[B":
            return "down"
        if seq == b"\x1b[C":
            return "right"
        if seq == b"\x1b[D":
            return "left"
        if seq in (b"\x1b[H", b"\x1b[5~"):
            return "up"
        if seq in (b"\x1b[F", b"\x1b[6~"):
            return "down"
        if seq == b"\x1b[Z":
            return "tab"
        return "esc"

    if b == b"\t":
        return "tab"
    if b in (b"\r", b"\n"):
        return "enter"
    if b == b" ":
        return "space"
    if b == b"\x03":
        return "ctrl_c"

    return b.decode("utf-8", errors="replace")


def _show_detail(record: dict, header: str, console: Console) -> bool:
    """Show the detail panel in full-screen. Returns False to quit, True to continue."""
    console.clear()
    console.print(_detail_view(record, header))
    while True:
        ch = _getch()
        if ch in ("q", "esc"):
            return True   # back to board
        if ch == "ctrl_c":
            return False  # quit entirely


# ── Terminal mode management ────────────────────────────────────────────────

def _enable_raw_mode() -> list:
    """Enable raw/cbreak terminal mode. Returns old settings for restore."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    # cbreak: no line buffering, signals still work, but echo off
    new[3] = new[3] & ~termios.ECHO   # disable echo
    new[3] = new[3] & ~termios.ICANON # disable canonical mode
    new[0] = new[0] & ~termios.INLCR  # disable NL→CR
    new[6][termios.VMIN] = 1           # min 1 byte before read returns
    new[6][termios.VTIME] = 0          # no timeout (blocking)
    termios.tcsetattr(fd, termios.TCSANOW, new)
    return old


def _restore_terminal(old):
    """Restore saved terminal settings."""
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
    except Exception:
        pass


# ── Main loop (synchronous, no Live) ────────────────────────────────────────

def _render_board(
    console: Console,
    records: list[dict],
    mode: str,
    cursor: int,
    other_label: str,
    other_count: int,
    category: str | None,
    prompt_id: str | None,
) -> None:
    """Print a single-table board to the terminal."""

    # ── Header ──
    is_best = mode == "best"
    label = BEST_LABEL if is_best else WORST_LABEL
    border = "green" if is_best else "red"

    hdr = Text()
    hdr.append(f" {label} ({len(records)}) ", style=f"bold {border}")
    hdr.append(
        Text(
            f"  [Tab] view {other_label} ({other_count})  |  "
            f"Cat: {category or 'all'}  Prompt: {prompt_id or 'all'}",
            style="dim",
        )
    )

    # ── Table ──
    lines: list[Text] = []
    # Column header
    h = Text()
    h.append("  Score  ", style="bold underline")
    h.append("Category      ", style="bold underline")
    h.append("Model              ", style="bold underline")
    h.append("Question          ", style="bold underline")
    h.append("Skill               ", style="bold underline")
    lines.append(h)

    for i, c in enumerate(records):
        row = _format_row(i, active=(i == cursor), c=c)
        lines.append(row)

    panel = Panel(
        Text("\n").join(lines),
        title=label,
        border_style=border,
        padding=(0, 0),
    )

    # ── Footer ──
    footer = Text(
        f"  ↑/↓: move ({cursor + 1}/{len(records)})  |  "
        f"Tab/←/→: switch to {other_label}  |  "
        "Enter/Space: detail  |  q: quit",
        style="dim",
    )

    # ── Render ──
    console.clear()
    console.print(hdr)
    console.print()
    console.print(panel)
    console.print()
    console.print(footer)


def run(
    worst: list[dict],
    best: list[dict],
    category: str | None,
    prompt_id: str | None,
) -> None:
    """Run the interactive score board loop."""
    mode: str = "best"   # start with best
    cursor: int = 0

    def _records():
        return best if mode == "best" else worst

    console = Console(force_terminal=True)

    old_term = _enable_raw_mode()
    try:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

        while True:
            records = _records()
            other_label = WORST_LABEL if mode == "best" else BEST_LABEL
            other_count = len(worst) if mode == "best" else len(best)

            _render_board(
                console, records, mode, cursor,
                other_label, other_count,
                category, prompt_id,
            )

            key = _getch()

            if key in ("ctrl_c", "q"):
                break

            elif key in ("tab", "right", "left"):
                mode = "worst" if mode == "best" else "best"
                records = _records()
                if records:
                    cursor = min(cursor, len(records) - 1)
                else:
                    cursor = 0

            elif key == "up":
                records = _records()
                if cursor > 0:
                    cursor -= 1

            elif key == "down":
                records = _records()
                if cursor < len(records) - 1:
                    cursor += 1

            elif key in ("enter", "space"):
                records = _records()
                if records and 0 <= cursor < len(records):
                    label = BEST_LABEL if mode == "best" else WORST_LABEL
                    header = f"{label}  —  #{cursor + 1} of {len(records)}"
                    if not _show_detail(records[cursor], header, console):
                        break

            else:
                continue

    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        _restore_terminal(old_term)
        console.clear()


# ── CLI entry point ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Interactive score board — browse worst/best calls",
    )
    parser.add_argument(
        "--category",
        help="Filter by category (fhir_query | hl7_transform | code_qa | general)",
    )
    parser.add_argument("--prompt-id", help="Filter by prompt_id")
    parser.add_argument(
        "--n",
        type=int,
        default=20,
        help="Records per bucket (default: 20)",
    )
    args = parser.parse_args()

    worst = get_worst_calls(args.n, args.category, args.prompt_id)
    best = get_best_calls(args.n, args.category, args.prompt_id)

    if not worst and not best:
        print("No scored calls found in Redis.")
        return

    try:
        run(worst, best, args.category, args.prompt_id)
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        Console().clear()
        print()


if __name__ == "__main__":
    main()
