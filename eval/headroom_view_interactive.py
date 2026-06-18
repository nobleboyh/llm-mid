"""Interactive headroom compression board — browse daily compression stats.

Usage:
    python -m eval.headroom_view_interactive

    # Show more/less days
    python -m eval.headroom_view_interactive --days 14

Keys:
    ↑ / ↓         Navigate days
    Enter / Space Show individual calls for selected day (Esc / q to go back)
    q / Ctrl+C    Quit
"""

from __future__ import annotations

import argparse
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

from eval.redis_store import (
    get_daily_headroom_stats,
    get_total_headroom_stats,
    get_day_headroom_calls,
)

log = logging.getLogger("eval.headroom_view_interactive")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_num(n: int) -> str:
    """Format integer with thousand separators."""
    return f"{n:,}"


def _fmt_pct(ratio: float) -> str:
    """Format ratio as percentage string."""
    return f"{ratio * 100:.1f}%"


# ── Row formatting ───────────────────────────────────────────────────────────

def _format_day_row(i: int, active: bool, d: dict) -> Text:
    """Return a single daily aggregate row as Rich Text."""
    style = "reverse" if active else ""
    s = Text()
    if active:
        s.append("▶ ", style="bold cyan")
    else:
        s.append("  ")

    s.append(f"{d['date']:<12}", style=style)
    s.append(f"{_fmt_num(d['call_count']):>6}", style=f"dim {style}" if not active else style)
    s.append(f"{_fmt_num(d['tokens_before']):>14}", style=style)
    s.append(f"{_fmt_num(d['tokens_after']):>14}", style=style)
    s.append(
        f"{_fmt_num(d['tokens_saved']):>14}",
        style=f"bold green {style}" if d["tokens_saved"] > 0 else f"dim {style}",
    )
    ratio = d.get("compression_ratio", 0)
    ratio_style = "bold yellow" if ratio > 0.5 else "yellow" if ratio > 0.2 else "dim"
    s.append(f"  {_fmt_pct(ratio):>7}", style=f"{ratio_style} {style}")
    return s


def _format_call_row(i: int, active: bool, c: dict) -> Text:
    """Return a single call row for the detail view."""
    style = "reverse" if active else ""
    s = Text()
    if active:
        s.append("▶ ", style="bold cyan")
    else:
        s.append("  ")

    model = c.get("model", "")
    ts = c.get("timestamp", "")
    time_str = ts[11:19] if len(ts) >= 19 else ts  # HH:MM:SS

    s.append(f"{time_str:<10}", style=style)
    s.append(f"{model[:24]:<24}" if len(model) <= 24 else f"{model[:23]}…", style=style)
    s.append(
        f"{_fmt_num(c['tokens_before']):>12}",
        style=f"dim {style}" if not active else style,
    )
    s.append(
        f"{_fmt_num(c['tokens_saved']):>12}",
        style=f"bold green {style}" if c.get("tokens_saved", 0) > 0 else f"dim {style}",
    )
    ratio = c.get("compression_ratio", 0)
    ratio_style = "bold yellow" if ratio > 0.5 else "yellow" if ratio > 0.2 else "dim"
    s.append(f"  {_fmt_pct(ratio):>7}", style=f"{ratio_style} {style}")

    transforms = c.get("transforms_applied", [])
    if transforms:
        tx = ", ".join(transforms)
        s.append(f"  {tx[:40]}" if len(tx) <= 40 else f"  {tx[:39]}…", style=f"dim {style}")
    return s


# ── Detail panel ─────────────────────────────────────────────────────────────

def _call_detail_panel(c: dict, header: str) -> Panel:
    """Build a Rich Panel showing full details for a single compression call."""
    lines: list[Text] = []
    lines.append(Text(header, style="bold white"))
    lines.append(Text(""))
    lines.append(Text.assemble(("Call ID:       ", "bold"), c.get("call_id", "-")))
    lines.append(
        Text.assemble(("Timestamp:     ", "bold"), c.get("timestamp", "-"))
    )
    lines.append(Text.assemble(("Model:         ", "bold"), c.get("model", "-")))
    lines.append(Text(""))
    lines.append(Text("Compression:", style="bold underline"))
    lines.append(
        Text.assemble(
            ("  Before:   ", "dim"),
            _fmt_num(c.get("tokens_before", 0)),
        )
    )
    lines.append(
        Text.assemble(
            ("  After:    ", "dim"),
            _fmt_num(c.get("tokens_after", 0)),
        )
    )
    lines.append(
        Text.assemble(
            ("  Saved:    ", "dim"),
            (_fmt_num(c.get("tokens_saved", 0)), " bold green"),
        )
    )
    ratio = c.get("compression_ratio", 0)
    ratio_color = "bold green" if ratio > 0.5 else "yellow"
    lines.append(
        Text.assemble(
            ("  Ratio:    ", "dim"),
            (_fmt_pct(ratio), f" {ratio_color}"),
        )
    )
    transforms = c.get("transforms_applied", [])
    lines.append(
        Text.assemble(
            ("  Transforms: ", "dim"),
            ", ".join(transforms) if transforms else "(none)",
            style="dim",
        )
    )
    lines.append(Text(""))
    lines.append(Text("  Esc / q → back    Ctrl+C → quit", style="dim"))

    body = Text("\n").join(lines)
    return Panel(body, border_style="cyan")


# ── Raw terminal input ──────────────────────────────────────────────────────

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


def _show_call_detail(call: dict, header: str, console: Console) -> bool:
    """Show a single call detail panel. Returns False to quit, True to continue."""
    console.clear()
    console.print(_call_detail_panel(call, header))
    while True:
        ch = _getch()
        if ch in ("q", "esc"):
            return True   # back to detail list
        if ch == "ctrl_c":
            return False  # quit entirely


_PAGE_SIZE = 10


def _show_day_detail(date_str: str, console: Console) -> bool:
    """Show the per-call detail view for a specific day, paginated to fit terminal.
    Returns False to quit, True to continue."""
    calls = get_day_headroom_calls(date_str)
    if not calls:
        console.clear()
        console.print(Text(f"No individual call records found for {date_str}.", style="yellow"))
        while True:
            ch = _getch()
            if ch in ("q", "esc", "enter", "space"):
                return True
            if ch == "ctrl_c":
                return False

    total = len(calls)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = 0               # current page index (0-based)
    cursor = 0             # cursor within the current page (0-based)

    def _page_bounds(pg: int) -> tuple[int, int]:
        """Return (start, end) indices for the given page."""
        start = pg * _PAGE_SIZE
        end = min(start + _PAGE_SIZE, total)
        return start, end

    while True:
        start, end = _page_bounds(page)
        # Clamp cursor if page changed and it's out of range
        page_size = end - start
        if cursor >= page_size:
            cursor = page_size - 1 if page_size > 0 else 0
        active_idx = start + cursor

        console.clear()

        # Header with range indicator
        hdr = Text()
        hdr.append(f" {date_str} ", style="bold cyan")
        hdr.append(
            f" — calls {start + 1}–{end} of {total}",
            style="dim",
        )
        console.print(hdr)
        console.print()

        # Table — only render the visible page
        lines: list[Text] = []
        col_hdr = Text()
        col_hdr.append("  Time      ", style="bold underline")
        col_hdr.append("Model                    ", style="bold underline")
        col_hdr.append("     Before  ", style="bold underline")
        col_hdr.append("      Saved  ", style="bold underline")
        col_hdr.append("  %      ", style="bold underline")
        col_hdr.append("Transforms", style="bold underline")
        lines.append(col_hdr)

        if total == 0:
            lines.append(Text("  (no calls)", style="dim italic"))
        else:
            for i in range(start, end):
                c = calls[i]
                lines.append(_format_call_row(i, active=(i == active_idx), c=c))

        panel = Panel(
            Text("\n").join(lines),
            title=f"Calls — {date_str}",
            border_style="cyan",
            padding=(0, 0),
        )
        console.print(panel)
        console.print()

        footer_parts = [
            f"  ↑/↓: move  ←/→: page  "
            f"({active_idx + 1}/{total})  |  "
            "Enter/Space: detail  |  "
            "Esc/q: back  |  Ctrl+C: quit",
        ]
        if total > _PAGE_SIZE:
            footer_parts.insert(
                0,
                f"  Page {page + 1}/{total_pages}",
            )
        footer = Text(" ".join(footer_parts) if len(footer_parts) > 1 else footer_parts[0], style="dim")
        console.print(footer)

        key = _getch()

        if key in ("ctrl_c",):
            return False
        if key in ("q", "esc"):
            return True
        elif key == "up":
            if cursor > 0:
                cursor -= 1
        elif key == "down":
            if cursor < page_size - 1:
                cursor += 1
        elif key == "left":
            if page > 0:
                page -= 1
                cursor = 0
        elif key == "right":
            if page < total_pages - 1:
                page += 1
                cursor = 0
        elif key in ("enter", "space"):
            if 0 <= active_idx < total:
                c = calls[active_idx]
                header = f"Compression Call  —  #{active_idx + 1} of {total}"
                if not _show_call_detail(c, header, console):
                    return False


# ── Terminal mode management ────────────────────────────────────────────────

def _enable_raw_mode() -> list:
    """Enable raw/cbreak terminal mode. Returns old settings for restore."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] = new[3] & ~termios.ECHO
    new[3] = new[3] & ~termios.ICANON
    new[0] = new[0] & ~termios.INLCR
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, new)
    return old


def _restore_terminal(old):
    """Restore saved terminal settings."""
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
    except Exception:
        pass


# ── Main render ─────────────────────────────────────────────────────────────

def _render_board(
    console: Console,
    totals: dict,
    days: list[dict],
    cursor: int,
    n_days: int,
) -> None:
    """Print the main daily stats board."""

    # ── Totals header ──
    hdr = Text()
    hdr.append(" HEADROOM COMPRESSION ", style="bold white on dark_green")
    hdr.append("  ")
    total_saved = totals.get("total_tokens_saved", 0)
    total_ratio = totals.get("compression_ratio", 0)
    hdr.append(
        f"Total saved: {_fmt_num(total_saved)} tokens ({_fmt_pct(total_ratio)})",
        style="bold yellow",
    )
    hdr.append(
        f"  |  {_fmt_num(totals.get('total_calls', 0))} calls",
        style="dim",
    )

    # ── Table ──
    lines: list[Text] = []
    col_hdr = Text()
    col_hdr.append("  Date        ", style="bold underline")
    col_hdr.append(" Calls", style="bold underline")
    col_hdr.append("       Before  ", style="bold underline")
    col_hdr.append("        After  ", style="bold underline")
    col_hdr.append("        Saved  ", style="bold underline")
    col_hdr.append("  %       ", style="bold underline")
    lines.append(col_hdr)

    if not days:
        lines.append(Text("  (no compression data yet)", style="dim italic"))

    for i, d in enumerate(days):
        row = _format_day_row(i, active=(i == cursor), d=d)
        lines.append(row)

    panel = Panel(
        Text("\n").join(lines),
        title=f"Daily Compression — last {n_days} days",
        border_style="green",
        padding=(0, 0),
    )

    # ── Footer ──
    footer = Text(
        f"  ↑/↓: move ({cursor + 1}/{max(1, len(days))})  |  "
        "Enter/Space: daily detail  |  "
        "q: quit",
        style="dim",
    )

    # ── Render ──
    console.clear()
    console.print(hdr)
    console.print()
    console.print(panel)
    console.print()
    console.print(footer)


# ── Main loop ────────────────────────────────────────────────────────────────

def run(days: list[dict], totals: dict, n_days: int) -> None:
    """Run the interactive compression stats board."""
    cursor = 0
    console = Console(force_terminal=True)

    old_term = _enable_raw_mode()
    try:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

        while True:
            _render_board(console, totals, days, cursor, n_days)

            key = _getch()

            if key in ("ctrl_c", "q"):
                break

            elif key == "up":
                if cursor > 0:
                    cursor -= 1

            elif key == "down":
                if cursor < len(days) - 1:
                    cursor += 1

            elif key in ("enter", "space"):
                if days and 0 <= cursor < len(days):
                    if not _show_day_detail(days[cursor]["date"], console):
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
        description="Interactive headroom compression stats board",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=10,
        help="Number of days to show (default: 10)",
    )
    args = parser.parse_args()

    days = get_daily_headroom_stats(args.days)
    totals = get_total_headroom_stats()

    if not days and totals.get("total_calls", 0) == 0:
        print("No headroom compression data found in Redis.")
        return

    try:
        run(days, totals, args.days)
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        Console().clear()
        print()


if __name__ == "__main__":
    main()
