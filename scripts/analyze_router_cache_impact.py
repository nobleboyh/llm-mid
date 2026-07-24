"""Interactive router cache-impact board — browse session model-switch data.

Usage:
  python -m scripts.analyze_router_cache_impact

  # Show more/less days
  python -m scripts.analyze_router_cache_impact --days 14

Keys:
  ↑ / ↓    Navigate sessions / scroll in detail
  ← / →    Page up / down
  Enter    Session detail view
  r        Report view (all PRD questions)
  Esc / q  Back / quit
  Ctrl+C   Quit
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import select
import sys
import termios

os.environ.setdefault("FORCE_COLOR", "1")
os.environ.setdefault("COLORTERM", "truecolor")
os.environ.setdefault("TERM", "xterm-256color")

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from eval.redis_store import r as redis_client

log = logging.getLogger("scripts.analyze_router_cache_impact")

# ── Cache pricing ($/M tokens) ──────────────────────────────────────────────
CACHE_PRICES = {
    "claude-sonnet-4-5":     {"write": 3.00, "read": 0.30},
    "claude-opus-4":         {"write": 15.00, "read": 1.50},
    "claude-haiku-4-5":      {"write": 1.00, "read": 0.10},
    "deepseek-v4-flash":     {"write": 0.50, "read": 0.05},
    "deepseek-v4-pro":       {"write": 2.00, "read": 0.20},
    "gemini-2.5-flash":      {"write": 0.30, "read": 0.03},
    "gemini-2.5-pro":        {"write": 2.50, "read": 0.25},
}
CACHE_TTL_SECONDS = 300  # 5 minutes — Anthropic default

# Map LiteLLM model aliases to pricing keys
MODEL_ALIAS_TO_PRICE_KEY = {
    "gemini-flash":   "gemini-2.5-flash",
    "gemini-pro":     "gemini-2.5-pro",
    "deepseek-flash": "deepseek-v4-flash",
    "deepseek-pro":   "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro":   "deepseek-v4-pro",
    "gemini/gemini-2.5-flash": "gemini-2.5-flash",
    "gemini/gemini-2.5-pro":   "gemini-2.5-pro",
    "deepseek/deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek/deepseek-v4-pro":   "deepseek-v4-pro",
}

_PAGE_SIZE = 10


# ── Helpers ─────────────────────────────────────────────────────────────────

def _fmt_num(n: int) -> str:
    """Format integer with thousand separators."""
    return f"{n:,}"


def _fmt_ts_last(ts: str) -> str:
    """Format ISO timestamp to compact MM-DD HH:MM display."""
    if len(ts) >= 16:
        return ts[5:10] + " " + ts[11:16]  # "2026-07-15T14:30:00" → "07-15 14:30"
    return ts


def _fmt_pct(ratio: float) -> str:
    """Format ratio as percentage string."""
    return f"{ratio * 100:.1f}%"


def _cache_status(read_tokens: int, created_tokens: int) -> tuple[str, str]:
    """Return (icon, style_name) for a single event's cache status.

    ✓ green   — Cache hit (provider returned cache_read > 0)
    ∆ yellow  — Cache written (no read, but new entry created)
    ✗ red     — Cold miss (no cache read or write)
    — dim     — No cache data available
    """
    if read_tokens > 0:
        return "✓", "green"
    if created_tokens > 0:
        return "∆", "yellow"
    return "✗", "red"


def _fmt_cache_pct(numerator: int, denominator: int) -> str:
    """Format a cache percentage, or '—' if no data."""
    if denominator <= 0:
        return "—"
    pct = numerator / denominator * 100
    return f"{pct:.1f}%"


def _cache_display(numerator: int, denominator: int) -> tuple[str, str]:
    """Return (display_str, style_name) for a cache percentage column."""
    if denominator <= 0:
        return "—", "dim"
    pct = numerator / denominator * 100
    if not numerator:
        return "0%", "dim"
    if pct > 50:
        return f"{pct:.1f}%", "bold green"
    return f"{pct:.1f}%", "yellow"


def _price_key(model_name: str) -> str | None:
    """Resolve a model name to a cache pricing key."""
    if model_name in MODEL_ALIAS_TO_PRICE_KEY:
        return MODEL_ALIAS_TO_PRICE_KEY[model_name]
    for key in CACHE_PRICES:
        if key in model_name:
            return key
    return None


def _cost_of_switch(hot_zone_tokens: int, to_model: str) -> float:
    """Estimate cost of a cache miss when switching to *to_model*."""
    pk = _price_key(to_model)
    if not pk:
        return 0.0
    prices = CACHE_PRICES[pk]
    diff = prices["write"] - prices["read"]
    return hot_zone_tokens * diff / 1_000_000


def _load_sessions(day_strs: list[str]) -> list[dict]:
    """Load all session summaries for given day strings.

    Returns list of session dicts with:
      session_key, events, switch_count, model_sequence, switch_pairs,
      total_cost, first_ts, last_ts, created_at
    """
    sessions: list[dict] = []
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(
            cursor=cursor, match="router:session:*", count=500,
        )
        for key in keys:
            if key.endswith(":meta") or key == "router:session:days":
                continue
            session_key = key.replace("router:session:", "")
            events_raw = redis_client.lrange(key, 0, -1)
            if not events_raw:
                continue
            events = [json.loads(e) for e in events_raw]
            # Events are newest-first (LPUSH), reverse to chronological
            events.reverse()

            model_seq = [e["model"] for e in events]
            switch_pairs: list[dict] = []
            switch_count = 0
            total_cost = 0.0
            for i in range(1, len(events)):
                prev = events[i - 1]
                curr = events[i]
                if prev["model"] != curr["model"]:
                    switch_count += 1
                    gap = curr.get("seconds_since_last")
                    within_ttl = gap is not None and gap <= CACHE_TTL_SECONDS
                    cost = 0.0
                    if within_ttl:
                        cost = _cost_of_switch(
                            curr.get("hot_zone_tokens", 0), curr["model"],
                        )
                        total_cost += cost
                    switch_pairs.append({
                        "from": prev["model"],
                        "to": curr["model"],
                        "turn": i,
                        "within_ttl": within_ttl,
                        "cost": cost,
                    })

            # ── Cache-hit percentage ────────────────────────────────────────
            # % of events where the provider confirmed cache_read > 0.
            cache_hit_count = sum(
                1 for ev in events
                if (ev.get("cache_read_input_tokens") or 0) > 0
            )
            cache_pct = cache_hit_count / max(1, len(events)) * 100

            sessions.append({
                "session_key": session_key,
                "events": events,
                "switch_count": switch_count,
                "model_sequence": model_seq,
                "switch_pairs": switch_pairs,
                "total_cost": total_cost,
                "first_ts": events[0]["timestamp"],
                "last_ts": events[-1]["timestamp"],
                "event_count": len(events),
                "cache_pct": cache_pct,
            })
        if cursor == 0:
            break
    sessions.sort(key=lambda s: s["last_ts"], reverse=True)
    return sessions


def _session_models_display(seq: list[str]) -> str:
    """Compact model sequence display."""
    if not seq:
        return "—"
    if len(set(seq)) == 1:
        return seq[0]
    # Show transitions
    parts = [seq[0]]
    for i in range(1, len(seq)):
        if seq[i] != seq[i - 1]:
            parts.append(seq[i])
    # Truncate if too long
    result = " → ".join(parts)
    if len(result) > 40:
        result = result[:37] + "…"
    return result


# ── TUI rendering ───────────────────────────────────────────────────────────

def _render_sessions_overview(
    sessions: list[dict], cursor: int, page: int, total_pages: int,
) -> Panel:
    """Render the main sessions list."""
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, len(sessions))
    page_sessions = sessions[start:end]

    total_switches = sum(s["switch_count"] for s in sessions)
    total_cost = sum(s["total_cost"] for s in sessions)

    lines: list[Text] = []

    # Header stats
    hdr = Text()
    hdr.append(f" ROUTER CACHE IMPACT ", style="bold cyan")
    hdr.append(
        f" — {len(sessions)} sessions | {total_switches} switches",
        style="dim",
    )
    if total_cost > 0:
        hdr.append(f" | est. cost: ${total_cost:.2f}", style="bold yellow")
    lines.append(hdr)
    lines.append(Text(""))

    # Column header
    col = Text()
    col.append(f" {'Session':<22}", style="bold underline")
    col.append(f"{'Sw':>4}", style="bold underline")
    col.append(f" {'Models':<37}", style="bold underline")
    col.append(f"{'Events':>8}", style="bold underline")
    col.append(f" {'Updated':<13}", style="bold underline")
    col.append(f"{'Cach%':>6}", style="bold underline")
    col.append(f"{'Cost':>8}", style="bold underline")
    lines.append(col)
    lines.append(Text(""))

    for i, s in enumerate(page_sessions):
        active = (i + start) == cursor
        style = "reverse" if active else ""
        row = Text()
        row.append("▶ " if active else "  ", style="bold cyan" if active else "")
        sk = s["session_key"]
        display_key = sk if len(sk) <= 20 else sk[:17] + "…"
        row.append(f"{display_key:<22}", style=style)
        row.append(f"{s['switch_count']:>4}", style=style)
        models = _session_models_display(s["model_sequence"])
        row.append(f" {models:<37}", style=style)
        row.append(f"{s['event_count']:>8}", style=style)
        last_updated = _fmt_ts_last(s.get("last_ts", ""))
        row.append(f" {last_updated:<13}", style=style)
        cache_pct = s.get("cache_pct", 0)
        cache_style = f"bold green {style}" if cache_pct >= 80 else f"yellow {style}" if cache_pct > 0 else f"dim {style}"
        row.append(f"{cache_pct:>5.0f}%", style=cache_style)
        cost_style = f"bold yellow {style}" if s["total_cost"] > 0 else f"dim {style}"
        row.append(f"${s['total_cost']:>7.2f}", style=cost_style)
        lines.append(row)

    body = Text("\n").join(lines)
    return Panel(body, border_style="green", padding=(0, 0))


def _render_session_detail(session: dict) -> Panel:
    """Render detail view for one session."""
    lines: list[Text] = []
    sk = session["session_key"]
    lines.append(Text(f" Session: {sk}", style="bold cyan"))
    lines.append(Text(
        f" {session['event_count']} requests | "
        f"{session['switch_count']} switches | "
        f"est. cost: ${session['total_cost']:.3f}",
        style="dim",
    ))
    lines.append(Text(""))

    col = Text()
    col.append(" #  ", style="bold underline")
    col.append("Time       ", style="bold underline")
    col.append("Model               ", style="bold underline")
    col.append("Prev Model          ", style="bold underline")
    col.append("Gap     ", style="bold underline")
    col.append("Cache", style="bold underline")
    col.append("Rd%    ", style="bold underline")
    col.append("Wr%    ", style="bold underline")
    col.append("TTL?", style="bold underline")
    lines.append(col)

    for i, e in enumerate(session["events"]):
        ts = e["timestamp"]
        time_str = ts[11:19] if len(ts) >= 19 else ts
        gap = e.get("seconds_since_last")
        gap_str = (
            f"{gap:.0f}s" if gap is not None and gap < 60
            else f"{gap / 60:.1f}m" if gap is not None
            else "—"
        )
        within = "✓" if gap is not None and gap <= CACHE_TTL_SECONDS else "✗" if gap is not None else "—"

        read_tokens = e.get("cache_read_input_tokens", 0) or 0
        created_tokens = e.get("cache_creation_input_tokens", 0) or 0
        total_tokens = e.get("total_prompt_tokens", 0) or 0

        cache_icon, cache_color = _cache_status(read_tokens, created_tokens)
        rd_str, rd_color = _cache_display(read_tokens, total_tokens)
        wr_str, wr_color = _cache_display(created_tokens, total_tokens)

        row = Text()
        row.append(f"{i + 1:>3} ", style="dim")
        row.append(f"{time_str:<11}")
        row.append(f"{e['model']:<20}")
        row.append(f"{e.get('previous_model') or '—':<20}")
        row.append(f"{gap_str:<8}")
        row.append(f"{cache_icon:<6}", style=cache_color)
        row.append(f" {rd_str:>6}", style=rd_color)
        row.append(f" {wr_str:>6}", style=wr_color)
        row.append(f"   {within}")
        lines.append(row)

    if session["switch_pairs"]:
        lines.append(Text(""))
        lines.append(Text("Switch pairs:", style="bold"))
        for sp in session["switch_pairs"]:
            ttl_flag = "within TTL" if sp["within_ttl"] else "expired"
            lines.append(Text(
                f"  {sp['from']} → {sp['to']} "
                f"(turn {sp['turn']}, {ttl_flag}, ${sp['cost']:.3f})",
                style="yellow" if sp["within_ttl"] else "dim",
            ))

    body = Text("\n").join(lines)
    return Panel(body, title=f"Session Detail", border_style="cyan")


def _render_report(sessions: list[dict]) -> Panel:
    """Render PRD §4 report."""
    if not sessions:
        return Panel(Text("No session data found.", style="yellow"), border_style="red")

    total_events = sum(s["event_count"] for s in sessions)
    total_switches = sum(s["switch_count"] for s in sessions)
    switch_rate = total_switches / total_events if total_events > 0 else 0
    total_cost = sum(s["total_cost"] for s in sessions)

    # Per-session switch distribution
    switch_counts = sorted([s["switch_count"] for s in sessions])
    p50 = switch_counts[len(switch_counts) // 2] if switch_counts else 0
    p90 = switch_counts[int(len(switch_counts) * 0.9)] if switch_counts else 0
    pmax = max(switch_counts) if switch_counts else 0

    # Inter-turn gaps
    all_gaps = []
    for s in sessions:
        for e in s["events"]:
            gap = e.get("seconds_since_last")
            if gap is not None:
                all_gaps.append(gap)
    all_gaps.sort()
    gap_p50 = all_gaps[len(all_gaps) // 2] if all_gaps else 0
    gap_p90 = all_gaps[int(len(all_gaps) * 0.9)] if all_gaps else 0
    gap_p99 = all_gaps[int(len(all_gaps) * 0.99)] if all_gaps else 0
    within_ttl_count = sum(1 for g in all_gaps if g <= CACHE_TTL_SECONDS)
    within_ttl_pct = within_ttl_count / len(all_gaps) if all_gaps else 0

    # Switch pair frequency
    pair_counts: dict[tuple[str, str], int] = {}
    for s in sessions:
        for sp in s["switch_pairs"]:
            key = (sp["from"], sp["to"])
            pair_counts[key] = pair_counts.get(key, 0) + 1
    top_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    lines: list[Text] = []
    lines.append(Text("═══ ROUTER CACHE IMPACT REPORT ═══", style="bold cyan"))
    lines.append(Text(""))

    lines.append(Text("Q1: Switch frequency", style="bold underline"))
    lines.append(Text(f"  Aggregate rate: {_fmt_pct(switch_rate)} of turns"))
    lines.append(Text(f"  Per-session switch count: p50={p50}  p90={p90}  max={pmax}"))
    if top_pairs:
        pairs_str = ", ".join(f"{f}→{t} ({c})" for (f, t), c in top_pairs[:5])
        lines.append(Text(f"  Top pairs: {pairs_str}"))
    lines.append(Text(""))

    lines.append(Text("Q2: Inter-turn gap", style="bold underline"))
    lines.append(Text(
        f"  p50={gap_p50:.0f}s  p90={gap_p90:.0f}s  p99={gap_p99:.0f}s"
    ))
    lines.append(Text(f"  {_fmt_pct(within_ttl_pct)} within {CACHE_TTL_SECONDS}s TTL"))
    lines.append(Text(""))

    lines.append(Text("Q3: Cache-hit verification", style="bold underline"))
    lines.append(Text("  (N/A — provider cache fields unavailable, Headroom estimate only)"))
    lines.append(Text(""))

    lines.append(Text("Q4: Lost-cache cost", style="bold underline"))
    lines.append(Text(f"  Total: ${total_cost:.2f} across {total_switches} switches"))
    for (f, t), count in top_pairs:
        cost = sum(
            sp["cost"] for s in sessions
            for sp in s["switch_pairs"]
            if sp["from"] == f and sp["to"] == t
        )
        lines.append(Text(f"  {f} → {t}: ${cost:.2f} ({count} switches)"))
    lines.append(Text(""))

    # Q5 needs total spend — we estimate from hot_zone + events
    total_hot_zone = sum(
        e.get("hot_zone_tokens", 0) for s in sessions for e in s["events"]
    )
    lines.append(Text("Q5: % of total spend (estimate)", style="bold underline"))
    lines.append(Text(f"  Est. lost-cache cost: ${total_cost:.2f}"))
    lines.append(Text(f"  Total hot-zone tokens: {_fmt_num(total_hot_zone)}"))
    lines.append(Text(""))

    lines.append(Text("Q6: Fingerprint spot-check", style="bold underline"))
    sample = sessions[:min(10, len(sessions))]
    for s in sample:
        lines.append(Text(
            f"  {s['session_key'][:24]}…  switches={s['switch_count']}  "
            f"events={s['event_count']}",
            style="dim",
        ))
    lines.append(Text(f"  Review {len(sample)} sessions for false negatives."))

    body = Text("\n").join(lines)
    return Panel(body, title="PRD §4 Report", border_style="yellow")


# ── Keyboard input ──────────────────────────────────────────────────────────

def _getch() -> str:
    """Read single keypress including escape sequences."""
    fd = sys.stdin.fileno()
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
        if seq in (b"\x1b[5~",):
            return "page_up"
        if seq in (b"\x1b[6~",):
            return "page_down"
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


# ── Terminal mode ───────────────────────────────────────────────────────────

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


def _disable_raw_mode(old: list) -> None:
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


# ── Run loop ────────────────────────────────────────────────────────────────

def run(sessions: list[dict]) -> None:
    console = Console(force_terminal=True)
    cursor = 0
    page = 0
    view = "sessions"  # "sessions" | "detail" | "report"
    detail_session_idx = 0

    old_term = _enable_raw_mode()
    try:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

        while True:
            total = len(sessions)
            total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
            if cursor >= total:
                cursor = max(0, total - 1)
            if page >= total_pages:
                page = total_pages - 1

            console.clear()

            if view == "sessions":
                panel = _render_sessions_overview(sessions, cursor, page, total_pages)
                console.print(panel)
                console.print()
                footer = Text(
                    f" ↑/↓: move ({cursor + 1}/{max(1, total)}) | "
                    f"←/→: page ({page + 1}/{total_pages}) | "
                    "Enter: detail | r: report | q: quit",
                    style="dim",
                )
                console.print(footer)

            elif view == "detail":
                s = sessions[detail_session_idx]
                panel = _render_session_detail(s)
                console.print(panel)
                console.print()
                footer = Text(
                    " ↑/↓: scroll | Esc/q: back | Ctrl+C: quit", style="dim",
                )
                console.print(footer)

            elif view == "report":
                panel = _render_report(sessions)
                console.print(panel)
                console.print()
                footer = Text(" Esc/q: back | Ctrl+C: quit", style="dim")
                console.print(footer)

            key = _getch()

            if key in ("ctrl_c",):
                return
            if key in ("q", "esc"):
                if view == "sessions":
                    return
                view = "sessions"
                continue

            if view == "sessions":
                start = page * _PAGE_SIZE
                page_size = min(_PAGE_SIZE, total - start)

                if key == "up" and cursor > 0:
                    cursor -= 1
                    if cursor < start:
                        page -= 1
                elif key == "down" and cursor < total - 1:
                    cursor += 1
                    if cursor >= start + _PAGE_SIZE:
                        page += 1
                elif key == "left" and page > 0:
                    page -= 1
                    cursor = page * _PAGE_SIZE
                elif key == "right" and page < total_pages - 1:
                    page += 1
                    cursor = page * _PAGE_SIZE
                elif key in ("enter", "space"):
                    if total > 0:
                        detail_session_idx = cursor
                        view = "detail"
                elif key == "r":
                    view = "report"

            elif view in ("detail", "report"):
                pass  # detail scroll could be added here; keep simple for Phase 0

    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        _disable_raw_mode(old_term)
        console.clear()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive router cache-impact board",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days", type=int, default=10,
        help="Number of past days to show (default: 10)",
    )
    args = parser.parse_args()

    sessions = _load_sessions([])  # Load all sessions regardless of day
    if not sessions:
        print("No router session data found in Redis.")
        return

    try:
        run(sessions)
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        Console().clear()
        print()


if __name__ == "__main__":
    main()
