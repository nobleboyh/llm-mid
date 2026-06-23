"""Interactive headroom compression board — browse daily compression stats.

Usage:
    python -m eval.headroom_view_interactive

    # Show more/less days
    python -m eval.headroom_view_interactive --days 14

Keys:
    ↑ / ↓         Navigate days / scroll prompt content in call detail
    ← / →         Jump to top / bottom of prompt content in call detail
    Shift+← / →   Scroll horizontally in call detail
    h / l         Scroll horizontally (vim-style)
    [ / ]         Page up / page down in call detail
    d             Toggle diff (Before → After) on/off in call detail
    b             Toggle full Before prompt on/off in call detail
    a             Toggle full After prompt on/off in call detail
    Enter / Space Show individual calls for selected day / page down in call detail
    Esc / q        Back
    Ctrl+C         Quit
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

import difflib
import json

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
    lines.append(Text("  Esc / q → back", style="dim"))

    body = Text("\n").join(lines)
    return Panel(body, border_style="cyan")


def _format_prompt_json(raw: str) -> str | None:
    """Pretty-print a JSON prompt string for display. Returns None on failure."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        # If it's a list of messages, pretty-print with indentation
        formatted = json.dumps(parsed, indent=2, ensure_ascii=False)
        return formatted
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw  # fall back to raw string


def _render_diff_panel(
    before_text: str | None,
    after_text: str | None,
    console: Console,
    scroll_offset: int,
    height: int,
    h_scroll: int = 0,
) -> Panel:
    """Render a unified-diff panel comparing before and after compression."""
    before_lines = (before_text or "").split("\n")
    after_lines = (after_text or "").split("\n")
    h_width = max(40, console.width - 12)

    diff_lines = list(difflib.unified_diff(
        before_lines, after_lines,
        n=0,  # no context — only actual changes
    ))

    # Skip the ---/+++ header lines (first 2 lines of unified diff)
    body_lines: list[Text] = []
    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            body_lines.append(Text(line, style="bold cyan"))
        elif line.startswith("+"):
            body_lines.append(Text(line, style="green"))
        elif line.startswith("-"):
            body_lines.append(Text(line, style="red"))
        else:
            sliced = line[h_scroll: h_scroll + h_width] if h_scroll > 0 else line[:h_width]
            body_lines.append(Text(sliced))

    window_lines = body_lines[scroll_offset: scroll_offset + height]
    body = Text("\n").join(window_lines)
    return Panel(body, title="Diff (Before → After)", border_style="cyan")


def _render_prompt_panels(
    prompt_before: str | None,
    prompt_after: str | None,
    console: Console,
    scroll_offset: int,
    height: int,
    h_scroll: int = 0,
) -> list[Panel]:
    """Render before/after prompt panels with vertical & horizontal scrolling.

    Args:
        prompt_before: Raw JSON string of original messages.
        prompt_after: Raw JSON string of compressed messages.
        console: Rich console (used to determine viewport width).
        scroll_offset: Vertical scroll offset (lines).
        height: Number of visible lines.
        h_scroll: Horizontal scroll offset (characters).

    Returns:
        List of Panels (one per side that has content), rendered as plain text.
    """
    panels: list[Panel] = []

    before_text = _format_prompt_json(prompt_before)
    after_text = _format_prompt_json(prompt_after)

    # Estimate available width per panel (accounting for border overhead)
    # Panel border: 3 left + 3 right = 6
    # Total overhead ≈ 12
    h_width = max(40, console.width - 12)

    for label, text_content, border_color in [
        ("Before Compression", before_text, "yellow"),
        ("After Compression", after_text, "green"),
    ]:
        if not text_content:
            continue
        lines = text_content.split("\n")

        # Slice vertically
        window_lines = lines[scroll_offset: scroll_offset + height]

        # Slice horizontally and build plain text lines — no syntax coloring
        rendered_lines: list[Text] = []
        for line in window_lines:
            sliced = line[h_scroll: h_scroll + h_width] if h_scroll > 0 else line[:h_width]
            rendered_lines.append(Text(sliced))

        body = Text("\n").join(rendered_lines)
        panels.append(Panel(body, title=label, border_style=border_color))

    return panels


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
        # Shift+arrows (used for horizontal scroll)
        if seq == b"\x1b[1;2D":
            return "shift_left"
        if seq == b"\x1b[1;2C":
            return "shift_right"
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
    """Show a single call detail panel with toggleable prompt views.

    Press d / b / a to toggle diff / before / after on and off.
    All off by default — just the stats.
    Returns False to quit, True to continue.
    """
    prompt_before = call.get("prompt_before")
    prompt_after = call.get("prompt_after")
    before_text = _format_prompt_json(prompt_before)
    after_text = _format_prompt_json(prompt_after)
    before_lines = (before_text or "").split("\n")
    after_lines = (after_text or "").split("\n")
    has_prompts = bool(prompt_before or prompt_after)

    h_width = max(40, console.width - 12)
    view_active: str | None = None  # "diff" | "before" | "after" | None
    scroll_offset = 0
    h_scroll = 0

    total_lines = 0
    max_line_len = 0
    max_scroll = 0
    max_h_scroll = 0

    prompt_height = max(5, min(30, console.height // 2 - 2))

    while True:
        console.clear()
        console.print(_call_detail_panel(call, header))

        if not has_prompts:
            console.print(Text("No prompt content stored for this call.", style="dim"))
        else:
            if view_active == "diff":
                panel = _render_diff_panel(
                    before_text, after_text, console,
                    scroll_offset, prompt_height, h_scroll,
                )
                console.print(panel)
            elif view_active == "before":
                panels = _render_prompt_panels(
                    before_text, after_text, console,
                    scroll_offset, prompt_height, h_scroll,
                )
                if panels:
                    console.print(panels[0])
            elif view_active == "after":
                panels = _render_prompt_panels(
                    before_text, after_text, console,
                    scroll_offset, prompt_height, h_scroll,
                )
                if len(panels) > 1:
                    console.print(panels[1])
                elif panels:
                    console.print(panels[0])
            else:
                console.print(Text("  d:diff  b:before  a:after  (press a key to show)", style="dim"))

            # Scroll indicators (only when something is visible and scrollable)
            if view_active and total_lines > prompt_height:
                page = scroll_offset // prompt_height + 1
                total_pages = (total_lines + prompt_height - 1) // prompt_height
                pct = scroll_offset / max_scroll if max_scroll > 0 else 0
                bar_width = 20
                filled = int(bar_width * pct)
                bar = "█" * filled + "░" * (bar_width - filled)
                console.print(Text(
                    f"  p.{page}/{total_pages} ↕ {scroll_offset + 1}–{min(scroll_offset + prompt_height, total_lines)}/{total_lines} {bar}",
                    style="dim",
                ))
            if view_active and max_h_scroll > 0:
                hpct = h_scroll / max_h_scroll if max_h_scroll > 0 else 0
                hbar_width = 16
                hfilled = int(hbar_width * hpct)
                hbar = "█" * hfilled + "░" * (hbar_width - hfilled)
                console.print(Text(
                    f"  ↔ col {h_scroll + 1}–{min(h_scroll + h_width, max_line_len)}/{max_line_len} {hbar}",
                    style="dim",
                ))

            # Toggle indicator line
            labels = {
                None: "(hidden)",
                "diff": "[Diff]",
                "before": "[Before]",
                "after": "[After]",
            }
            status = labels.get(view_active, "")
            hints = f"  {status}  d:diff  b:before  a:after  []:pg  ↑↓:scroll  ↔:pan"
            console.print(Text(hints, style="dim"))

        ch = _getch()

        if ch in ("q", "esc"):
            return True
        if ch == "ctrl_c":
            return False

        # Exclusive view switching — press again to hide
        if ch == "d" and has_prompts:
            if view_active == "diff":
                view_active = None
                total_lines = 0; max_line_len = 0
            else:
                view_active = "diff"
                diff_lines = list(difflib.unified_diff(before_lines, after_lines, n=0))
                diff_lines_clean = [l for l in diff_lines if not l.startswith("---") and not l.startswith("+++")]
                total_lines = len(diff_lines_clean)
                max_line_len = max((len(l) for l in diff_lines_clean), default=0)
            scroll_offset = 0; h_scroll = 0
            max_scroll = max(0, total_lines - prompt_height)
            max_h_scroll = max(0, max_line_len - h_width)
        elif ch == "b" and has_prompts:
            if view_active == "before":
                view_active = None
                total_lines = 0; max_line_len = 0
            else:
                view_active = "before"
                total_lines = len(before_lines)
                max_line_len = max((len(l) for l in before_lines), default=0)
            scroll_offset = 0; h_scroll = 0
            max_scroll = max(0, total_lines - prompt_height)
            max_h_scroll = max(0, max_line_len - h_width)
        elif ch == "a" and has_prompts:
            if view_active == "after":
                view_active = None
                total_lines = 0; max_line_len = 0
            else:
                view_active = "after"
                total_lines = len(after_lines)
                max_line_len = max((len(l) for l in after_lines), default=0)
            scroll_offset = 0; h_scroll = 0
            max_scroll = max(0, total_lines - prompt_height)
            max_h_scroll = max(0, max_line_len - h_width)
        # Page-based navigation ([ / ])
        elif ch == "[" and scroll_offset > 0:
            scroll_offset = max(0, scroll_offset - prompt_height)
        elif ch == "]" and scroll_offset < max_scroll:
            scroll_offset = min(max_scroll, scroll_offset + prompt_height)
        # Vertical scrolling
        elif ch == "up" and scroll_offset > 0:
            scroll_offset -= 1
        elif ch == "down" and scroll_offset < max_scroll:
            scroll_offset += 1
        elif ch == "space":
            scroll_offset = min(scroll_offset + prompt_height, max_scroll)
        elif ch == "left":
            scroll_offset = 0
        elif ch == "right":
            scroll_offset = max_scroll
        # Horizontal scrolling
        elif ch in ("shift_left", "h") and h_scroll > 0:
            h_scroll = max(0, h_scroll - 8)
        elif ch in ("shift_right", "l") and h_scroll < max_h_scroll:
            h_scroll = min(max_h_scroll, h_scroll + 8)


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
