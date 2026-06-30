# UI/UX Guidelines — GateMid

GateMid's user-facing interfaces are terminal-based (TUI). There is no web UI, no GUI. This document covers the interactive CLI tools in `eval/`.

## Design principles

1. **Keyboard-first** — everything navigable without a mouse. Arrow keys, Tab, Enter, Escape, and single-letter hotkeys.
2. **Information density** — show as much useful data as fits on screen without scrolling.
3. **Color as signal** — green = good, red = bad, yellow = attention, cyan = active/selected.
4. **Reversible navigation** — Esc always goes back, q always quits, no trapped states.
5. **Raw terminal mode** — disable line buffering and echo for instant key response; restore on exit.

## Shared TUI infrastructure

Both interactive tools share terminal handling code (duplicated for independence, by design — no shared TUI framework abstraction):

### Terminal mode management
```python
# Enter raw mode — disable echo, canonical mode, line buffering
old = termios.tcgetattr(fd)
new[3] = new[3] & ~termios.ECHO    # no echo
new[3] = new[3] & ~termios.ICANON  # no line buffering
new[6][VMIN] = 1                    # blocking read
new[6][VTIME] = 0                   # no timeout
termios.tcsetattr(fd, TCSANOW, new)
```

### Key input parsing
- `_getch()` reads raw bytes, parses escape sequences
- Returns semantic key names: `"up"`, `"down"`, `"q"`, `"enter"`, `"space"`, `"tab"`, etc.
- Handles ESC sequences with select-based timeout to distinguish `Esc` from arrow keys
- Cursor hidden during TUI: `\033[?25l` / `\033[?25h`

### Always restore terminal
```python
finally:
    sys.stdout.write("\033[?25h")   # show cursor
    _restore_terminal(old_term)      # restore terminal settings
    console.clear()                  # clear screen
```

## Score board (`eval.cli score`)

### Layout

```
 ─────────────────────────────────────────────────────
  ✓ BEST SCORING (20)   [Tab] view ⚠ WORST SCORING (20)
 ─────────────────────────────────────────────────────
 ┌────────────────────────────────────────────────────┐
 │ ✓  BEST SCORING                                    │
 │                                                    │
 │   Score  Category      Model              Question │
 │ ▶ 0.923  general        deepseek-pro       How do  │
 │   0.887  code_qa        deepseek-pro       Explain │
 │   0.851  general        deepseek-flash     What is │
 │   ...                                              │
 └────────────────────────────────────────────────────┘
  ↑/↓: move (1/20)  |  Tab/←/→: switch  |  Enter: detail  |  q: quit
```

### Views
- **Best** (default): highest composite scores, descending
- **Worst**: lowest composite scores, ascending
- **Detail**: full record — call ID, score, category, model, tokens, timestamp, per-dimension scores, full question, full answer

### Navigation
| Key | Action |
|-----|--------|
| `↑`/`↓` | Move cursor through records |
| `Tab` / `←` / `→` | Toggle Best ↔ Worst |
| `Enter` / `Space` | Open detail view |
| `Esc` / `q` (in detail) | Return to board |
| `q` (in board) | Quit |

### Color coding
- Selected row: reverse video
- Scores: bold yellow
- Skill names: bold magenta
- Best panel: green border
- Worst panel: red border

## Headroom compression board (`eval.cli headroom`)

### Layout

```
 HEADROOM COMPRESSION   Total saved: 1,234,567 tokens (72.3%)  |  3,456 calls
 ─────────────────────────────────────────────────────────────────────
 ┌────────────────────────────────────────────────────────────────────┐
 │ Daily Compression — last 10 days                                   │
 │                                                                    │
 │   Date         Calls       Before        After        Saved       %│
 │ ▶ 2026-06-30      42      123,456       34,567       88,889  72.0%│
 │   2026-06-29      38      98,765        27,654       71,111  72.0%│
 │   ...                                                              │
 └────────────────────────────────────────────────────────────────────┘
  ↑/↓: move (1/10)  |  Enter/Space: daily detail  |  q: quit
```

### Day detail view

```
 2026-06-30 — calls 1–10 of 42
 ┌────────────────────────────────────────────────────────────────────┐
 │ Calls — 2026-06-30                                                 │
 │                                                                    │
 │   Time      Model                     Before       Saved       %   │
 │ ▶ 14:23:05  deepseek-pro              5,432       3,876    71.3%  │
 │   14:22:18  deepseek-flash            2,100       1,512    72.0%  │
 └────────────────────────────────────────────────────────────────────┘
  Page 1/5  ↑/↓: move  ←/→: page  (1/42)  |  Enter/Space: detail
```

### Call detail view

Shows full compression call data:
- Call ID, timestamp, model
- Tokens before/after/saved, compression ratio
- Transforms applied (SmartCrusher, CodeCompressor, CacheAligner)
- Skill injection info (if any)

**Toggleable prompt views** (press key to show):
- `d` — Unified diff (Before → After)
- `b` — Full Before prompt (JSON pretty-printed)
- `a` — Full After prompt (JSON pretty-printed)

### Navigation (day detail)
| Key | Action |
|-----|--------|
| `↑`/`↓` | Move cursor through calls |
| `←`/`→` | Previous/next page |
| `Enter`/`Space` | Open call detail |
| `Esc`/`q` | Back to days |

### Navigation (call detail with prompt)
| Key | Action |
|-----|--------|
| `d`/`b`/`a` | Toggle diff/before/after view |
| `↑`/`↓` | Vertical scroll |
| `←`/`→` | Jump to top/bottom |
| `Shift+←`/`Shift+→` or `h`/`l` | Horizontal scroll |
| `[`/`]` or `Space` | Page up/down |
| `Esc`/`q` | Back to call list |

### Scroll bars
- Vertical: `█`/`░` bar showing position in document
- Horizontal: same pattern showing horizontal position
- Page indicator: `p.2/5 ↕ 31–60/200`

### Color coding
- Saved tokens: bold green (positive) or dim (zero)
- Compression ratio: bold yellow (>50%), yellow (>20%), dim (≤20%)
- Skill names: bold magenta
- Diff: green (+), red (-), cyan (@@)
- Active row: reverse video

## CLI entry points

```
python -m eval.cli score [--n 50] [--category ...] [--prompt-id ...]
python -m eval.cli headroom [--days 14]
python -m eval.cli clear-redis [--hard]
python -m eval.cli --help
```

## Non-interactive output

For programmatic use, import directly:
```python
from eval.redis_store import get_best_calls, get_worst_calls, get_daily_headroom_stats
# Returns list[dict], no TUI
```

## Rich library usage

Both tools use [Rich](https://github.com/Textualize/rich) for rendering:
- `Console(force_terminal=True)` — ensures color output even when piped
- `Panel` — bordered containers with titles
- `Text` — styled text with `Text.assemble()` for inline styles
- No `Live` display — synchronous rendering, simpler and more reliable

## Terminal compatibility

- Requires true color support (`COLORTERM=truecolor`)
- `xterm-256color` minimum
- ANSI escape codes for cursor hiding and raw mode
- Fallback: `FORCE_COLOR=1` ensures Rich outputs colors
- Requires `-it` (interactive TTY) for TUI mode
