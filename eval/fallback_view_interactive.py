"""Interactive fallback event board — browse daily fallback stats.

Usage:
    python -m eval.fallback_view_interactive

    # Show more/less days
    python -m eval.fallback_view_interactive --days 14

Keys:
    ↑ / ↓         Navigate days / scroll in call detail
    ← / →         Page navigation in call detail
    Enter / Space  Show individual fallback events for selected day
    q / Esc       Back (or quit from top level)
    Ctrl+C        Quit
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
    get_daily_fallback_stats,
    get_total_fallback_stats,
    get_day_fallback_calls,
)

log = logging.getLogger("eval.fallback_view_interactive")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_num(n: int) -> str:
    return f"{n:,}"


# ── Row formatting ───────────────────────────────────────────────────────────

def _format_day_row(i: int, active: bool, d: dict) -> Text:
    """Return a single daily aggregate row."""
    style = "reverse" if active else ""
    s = Text()
    if active:
        s.append("▶ ", style="bold cyan")
    else:
        s.append("  ")

    s.append(f"{d['date']:<12}", style=style)
    cnt = d["call_count"]
    cnt_style = "bold red" if cnt > 0 else "dim"
    s.append(f"{_fmt_num(cnt):>6}", style=f"{cnt_style} {style}")
    return s


def _format_call_row(i: int, active: bool, c: dict) -> Text:
    """Return a single call row for the detail view."""
    style = "reverse" if active else ""
    s = Text()
    if active:
        s.append("▶ ", style="bold cyan")
    else:
        s.append("  ")

    ts = c.get("timestamp", "")
    time_str = ts[11:19] if len(ts) >= 19 else ts  # HH:MM:SS

    fb_type = c.get("fallback_type", "order")
    fb_label = "Order" if fb_type == "order" else "X-Model"
    order_val = c.get("order", 0)
    if order_val:
        fb_label += f" #{order_val}"

    mg = c.get("model_group", "")
    dm = c.get("deployment_model", "")
    exc = c.get("original_exception", "")

    s.append(f"{time_str:<10}", style=style)
    s.append(f"{fb_label:<10}", style=f"bold {'yellow' if fb_type == 'cross-model' else 'red'} {style}")
    s.append(f"{mg[:20]:<20}", style=style)
    s.append(f"→ {dm[:28]:<28}", style=f"bold green {style}" if len(dm) <= 28 else f"bold green {style}")
    if exc:
        s.append(f"  {exc[:40]}", style=f"dim {style}")
    return s


# ── Detail panel ─────────────────────────────────────────────────────────────

def _call_detail_panel(c: dict) -> Panel:
    """Build a Rich Panel showing full details for a single fallback event."""
    lines: list[Text] = []
    lines.append(Text("Fallback Event", style="bold white"))
    lines.append(Text(""))
    lines.append(
        Text.assemble(("Call ID:            ", "bold"), c.get("call_id", "-"))
    )
    lines.append(
        Text.assemble(("Timestamp:          ", "bold"), c.get("timestamp", "-"))
    )
    lines.append(
        Text.assemble(
            ("Fallback Type:      ", "bold"),
            (c.get("fallback_type", "-"), "bold yellow"),
        )
    )
    order_val = c.get("order", 0)
    if order_val:
        lines.append(
            Text.assemble(
                ("Deployment Order:   ", "bold"),
                (str(order_val), "bold red"),
            )
        )
    lines.append(
        Text.assemble(
            ("Model Group:        ", "bold"),
            c.get("model_group", "-"),
        )
    )
    lines.append(
        Text.assemble(
            ("Served by:          ", "bold"),
            (c.get("deployment_model", "-"), "bold green"),
        )
    )
    exc = c.get("original_exception", "")
    if exc:
        lines.append(Text(""))
        lines.append(Text("Original Exception:", style="bold underline"))
        lines.append(Text(f"  {exc}", style="dim"))
    lines.append(Text(""))
    lines.append(Text("  q / Esc → back", style="dim"))

    body = Text("\n").join(lines)
    return Panel(body, border_style="yellow")


# ── Raw terminal input ──────────────────────────────────────────────────────

def _getch() -> str:
    """Read a single keypress from stdin (expects terminal in raw mode)."""
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


# ── Terminal mode management ────────────────────────────────────────────────

def _enable_raw_mode() -> list:
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
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
    except Exception:
        pass


# ── Detail view (per-day call list) ──────────────────────────────────────────

_PAGE_SIZE = 10


def _show_day_detail(date_str: str, console: Console) -> bool:
    """Show per-call detail view for a specific day.
    Returns False to quit, True to continue."""
    calls = get_day_fallback_calls(date_str)
    if not calls:
        console.clear()
        console.print(Text(f"No fallback events found for {date_str}.", style="yellow"))
        while True:
            ch = _getch()
            if ch in ("q", "esc", "enter", "space"):
                return True
            if ch == "ctrl_c":
                return False

    total = len(calls)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = 0
    cursor = 0

    def _page_bounds(pg: int) -> tuple[int, int]:
        start = pg * _PAGE_SIZE
        end = min(start + _PAGE_SIZE, total)
        return start, end

    while True:
        start, end = _page_bounds(page)
        page_size = end - start
        if cursor >= page_size:
            cursor = page_size - 1 if page_size > 0 else 0
        active_idx = start + cursor

        console.clear()

        hdr = Text()
        hdr.append(f" {date_str} ", style="bold cyan")
        hdr.append(f" — events {start + 1}–{end} of {total}", style="dim")
        console.print(hdr)
        console.print()

        lines: list[Text] = []
        col_hdr = Text()
        col_hdr.append("  Time      ", style="bold underline")
        col_hdr.append("Type      ", style="bold underline")
        col_hdr.append("Model Group         ", style="bold underline")
        col_hdr.append("Served By                        ", style="bold underline")
        lines.append(col_hdr)

        if total == 0:
            lines.append(Text("  (no events)", style="dim italic"))
        else:
            for i in range(start, end):
                c = calls[i]
                lines.append(_format_call_row(i, active=(i == active_idx), c=c))

        panel = Panel(
            Text("\n").join(lines),
            title=f"Fallback Events — {date_str}",
            border_style="yellow",
            padding=(0, 0),
        )
        console.print(panel)
        console.print()

        footer_parts = [
            f"  ↑/↓: move  ←/→: page  "
            f"({active_idx + 1}/{total})  |  "
            "Enter/Space: detail  |  "
            "q: back  |  Ctrl+C: quit",
        ]
        if total > _PAGE_SIZE:
            footer_parts.insert(
                0,
                f"  Page {page + 1}/{total_pages}",
            )
        footer = Text(
            " ".join(footer_parts) if len(footer_parts) > 1 else footer_parts[0],
            style="dim",
        )
        console.print(footer)

        key = _getch()

        if key == "ctrl_c":
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
                console.clear()
                console.print(_call_detail_panel(c))
                while True:
                    ch = _getch()
                    if ch in ("q", "esc", "enter", "space"):
                        break
                    if ch == "ctrl_c":
                        return False


# ── Main render ─────────────────────────────────────────────────────────────

def _render_board(
    console: Console,
    totals: dict,
    days: list[dict],
    cursor: int,
    n_days: int,
) -> None:
    """Print the main daily fallback stats board."""

    # ── Header ──
    hdr = Text()
    hdr.append(" FALLBACK EVENTS ", style="bold white on dark_red")
    hdr.append("  ")
    total_calls = totals.get("total_calls", 0)
    hdr.append(
        f"Total fallbacks: {_fmt_num(total_calls)}",
        style="bold yellow",
    )

    # ── Table ──
    lines: list[Text] = []
    col_hdr = Text()
    col_hdr.append("  Date        ", style="bold underline")
    col_hdr.append(" Events", style="bold underline")
    lines.append(col_hdr)

    if not days:
        lines.append(Text("  (no fallback data yet)", style="dim italic"))

    for i, d in enumerate(days):
        row = _format_day_row(i, active=(i == cursor), d=d)
        lines.append(row)

    total_calls_str = _fmt_num(total_calls)
    panel = Panel(
        Text("\n").join(lines),
        title=f"Daily Fallbacks — last {n_days} days | Total: {total_calls_str}",
        border_style="yellow",
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
    """Run the interactive fallback events board."""
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
        description="Interactive fallback events board",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=10,
        help="Number of days to show (default: 10)",
    )
    args = parser.parse_args()

    days = get_daily_fallback_stats(args.days)
    totals = get_total_fallback_stats()

    if not days and totals.get("total_calls", 0) == 0:
        print("No fallback data found in Redis.")
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
