"""Shared terminal utilities for interactive TUI scripts."""

from __future__ import annotations

import os
import select
import sys
import termios
from typing import Any


def enable_raw_mode() -> list[Any]:
    """Enable raw/cbreak terminal mode. Return saved settings for restore."""
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


def restore_terminal(old: list[Any]) -> None:
    """Restore saved terminal settings."""
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
    except Exception:
        pass


def getch() -> str:
    """Read a single keypress from stdin (expects terminal in raw mode).

    Returns: "up", "down", "left", "right", "tab", "enter", "space",
             "esc", "ctrl_c", "shift_left", "shift_right", "home", "end",
             or the decoded UTF-8 character.
    """
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
            rest += os.read(fd, 1)
        seq = rest
        if seq == b"[A":
            return "up"
        if seq == b"[B":
            return "down"
        if seq == b"[D":
            return "left"
        if seq == b"[C":
            return "right"
        if seq in (b"[H", b"[1~"):
            return "home"
        if seq in (b"[F", b"[4~"):
            return "end"
        if seq in (b"[5~", b"[I"):
            return "page_up"
        if seq in (b"[6~", b"[G"):
            return "page_down"
        if seq == b"[Z":
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
