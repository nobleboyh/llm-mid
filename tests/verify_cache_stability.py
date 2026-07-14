#!/usr/bin/env python3
"""Prompt caching efficiency verification — Part 1 empirical tests.

Usage:
    # Terminal 1: Start the mock capture server
    python tests/verify_cache_mock_server.py

    # Terminal 2: Run this test harness
    python tests/verify_cache_stability.py

    The test harness will:
    1. Reconfigure GateMid to route through the mock server
    2. Run all 8 test cases (A-H)
    3. Restore the original configuration
    4. Analyze captured payloads
"""

import hashlib
import json
import difflib
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

# ── Configuration ────────────────────────────────────────────────────────
GATEMID_URL = os.environ.get("GATEMID_URL", "http://localhost:4000")
MOCK_URL = "http://host.docker.internal:18000"
GATEWAY_KEY = os.environ.get("GATEWAY_MASTER_KEY", "sk-local-dev-key")
CAPTURES_DIR = Path("./captures")
CONFIG_PATH = Path("./litellm_config.yaml")
CONFIG_BACKUP_PATH = Path("./litellm_config.yaml.bak")

HEADERS = {
    "Authorization": f"Bearer {GATEWAY_KEY}",
    "Content-Type": "application/json",
}

# Conversation template (simulates a coding agent conversation)
SYSTEM_PROMPT = "You are a helpful coding assistant. Always write clean, well-documented code."
TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the filesystem",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
]

# Base conversation — a "session" with history
BASE_MESSAGES = [
    {"role": "user", "content": "Hello, what can you do?"},
    {"role": "assistant", "content": "I can help you read and write files, write code, and answer questions."},
    {"role": "user", "content": "Create a Python script that reads a CSV file and plots the data."},
    {"role": "assistant", "content": "I'll create a CSV plotter script for you. Let me first check if there's sample data available.\n\n```python\nimport csv\nimport matplotlib.pyplot as plt\n\ndef plot_csv(filepath, x_column, y_column):\n    with open(filepath, 'r') as f:\n        reader = csv.DictReader(f)\n        data = list(reader)\n    x = [row[x_column] for row in data]\n    y = [row[y_column] for row in data]\n    plt.plot(x, y)\n    plt.show()\n\nplot_csv('data.csv', 'date', 'value')\n```"},
]

LARGE_TOOL_OUTPUT = json.dumps({
    "users": [{"id": i, "name": f"User_{i}", "email": f"user{i}@example.com", "role": "admin" if i % 3 == 0 else "user"} for i in range(500)],
    "orders": [{"id": i, "amount": i * 10.5, "status": ["pending", "completed", "canceled"][i % 3]} for i in range(1000)],
})


# ── Test Harness ─────────────────────────────────────────────────────────

def hash_prefix(body: dict, prefix_chars: int | None = None) -> str:
    """Hash the serialized request body, or a leading slice of it."""
    raw = json.dumps(body, sort_keys=False)
    if prefix_chars:
        raw = raw[:prefix_chars]
    return hashlib.sha256(raw.encode()).hexdigest()


def split_hot_and_live(body: dict) -> tuple[dict, dict]:
    """Split a captured request into 'hot zone' and 'live zone'.

    Hot zone: system prompt, tools, all messages except the last.
    Live zone: the latest user message or tool result.
    """
    messages = body.get("messages", [])
    hot = {
        "system": body.get("system"),
        "tools": body.get("tools"),
        "messages": messages[:-1] if len(messages) > 1 else messages,
    }
    live = {
        "messages": messages[-1:] if messages else [],
    }
    return hot, live


def diff_report(name: str, body_a: dict, body_b: dict):
    """Compare hot zones from two captured requests."""
    hot_a, live_a = split_hot_and_live(body_a)
    hot_b, live_b = split_hot_and_live(body_b)

    hot_a_s = json.dumps(hot_a, sort_keys=False, indent=2)
    hot_b_s = json.dumps(hot_b, sort_keys=False, indent=2)

    identical = hot_a_s == hot_b_s
    print(f"  [{name}] hot zone byte-identical: {identical}")
    print(f"  [{name}] hot zone hash A:     {hash_prefix(hot_a)}")
    print(f"  [{name}] hot zone hash B:     {hash_prefix(hot_b)}")
    print(f"  [{name}] hot zone first-500 A: {hash_prefix(hot_a, prefix_chars=500)}")
    print(f"  [{name}] hot zone first-500 B: {hash_prefix(hot_b, prefix_chars=500)}")

    if not identical:
        print(f"  [{name}] HOT ZONE DIFF (this should be EMPTY for most tests!):")
        diff = difflib.unified_diff(
            hot_a_s.splitlines(), hot_b_s.splitlines(),
            lineterm="", fromfile="call_a_hot", tofile="call_b_hot",
        )
        for line in diff:
            print(f"    {line}")

    live_diff = json.dumps(live_a, sort_keys=False) != json.dumps(live_b, sort_keys=False)
    print(f"  [{name}] live zone differs (expected True for B, C, F, H): {live_diff}")
    print()

    return identical


# ── Gateway Client ───────────────────────────────────────────────────────

def send_request(payload: dict, model: str = "gemini-flash") -> dict:
    """Send a request through GateMid and return the response."""
    client_payload = {
        "model": model,
        "messages": payload.get("messages", payload),
    }
    if "system" in payload:
        client_payload["system"] = payload["system"]
    if "tools" in payload:
        client_payload["tools"] = payload["tools"]

    resp = httpx.post(
        f"{GATEMID_URL}/v1/chat/completions",
        headers=HEADERS,
        json=client_payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


# ── Test Cases ────────────────────────────────────────────────────────────

def make_payload(messages: list, *, system: str | None = SYSTEM_PROMPT,
                 tools: list | None = None) -> dict:
    """Build an OpenAI-format payload with system + messages + tools."""
    p = {"messages": messages}
    if system:
        p["messages"].insert(0, {"role": "system", "content": system})
    if tools:
        p["tools"] = tools
    return p


def test_a_baseline_repeat():
    """A: Same exact conversation, same model — hot zone must be byte-identical."""
    print("\n=== Test A: Baseline Repeat ===")
    payload = make_payload(BASE_MESSAGES.copy())

    resp1 = send_request(payload)
    resp2 = send_request(payload)

    # We captured them on the mock server — return identifiers for analysis
    return ("A", [resp1, resp2])


def test_b_new_user_turn():
    """B: Same conversation + one more user turn — hot zone identical, live zone differs."""
    print("\n=== Test B: New User Turn Appended ===")
    base = BASE_MESSAGES.copy()

    payload_a = make_payload(base)
    base_b = base + [{"role": "user", "content": "Can you add error handling to that script?"}]
    payload_b = make_payload(base_b)

    resp1 = send_request(payload_a)
    resp2 = send_request(payload_b)

    return ("B", [resp1, resp2])


def test_c_large_tool_output():
    """C: Last turn has large tool result — hot zone identical, live zone differs (compressed)."""
    print("\n=== Test C: Large Tool Output ===")
    base = BASE_MESSAGES.copy()

    payload_a = make_payload(base)
    base_b = base + [{"role": "assistant", "content": "Let me read the file..."},
                      {"role": "user", "content": LARGE_TOOL_OUTPUT}]
    payload_b = make_payload(base_b)

    resp1 = send_request(payload_a)
    resp2 = send_request(payload_b)

    return ("C", [resp1, resp2])


def test_d_timestamp_same_day():
    """D: System prompt with date, same day — hot zone should be byte-identical (CacheAligner extracts date)."""
    print("\n=== Test D: Timestamp in System Prompt (Same Day) ===")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    system_a = f"{SYSTEM_PROMPT}\n\nToday is {today}."
    system_b = f"{SYSTEM_PROMPT}\n\nToday is {today}."

    payload_a = make_payload(BASE_MESSAGES.copy(), system=system_a)
    payload_b = make_payload(BASE_MESSAGES.copy(), system=system_b)

    resp1 = send_request(payload_a)
    resp2 = send_request(payload_b)

    return ("D", [resp1, resp2])


def test_e_timestamp_different_day():
    """E: Timestamps from two different days — diff should be localized to just the date."""
    print("\n=== Test E: Timestamp Different Days ===")

    # Simulate two different days
    day1 = "January 15, 2026"
    day2 = "January 16, 2026"

    system_a = f"{SYSTEM_PROMPT}\n\nToday is {day1}."
    system_b = f"{SYSTEM_PROMPT}\n\nToday is {day2}."

    payload_a = make_payload(BASE_MESSAGES.copy(), system=system_a)
    payload_b = make_payload(BASE_MESSAGES.copy(), system=system_b)

    resp1 = send_request(payload_a)
    resp2 = send_request(payload_b)

    return ("E", [resp1, resp2])


def test_f_same_model_router():
    """F: Complexity router picks same model for similar scores — hot zone identical."""
    print("\n=== Test F: Same Model from Router ===")
    # Both are "simple" queries that should route to gemini-flash
    payload_a = make_payload([{"role": "user", "content": "Write hello world in Python."}],
                             system=SYSTEM_PROMPT)
    payload_b = make_payload([{"role": "user", "content": "Write FizzBuzz in JavaScript."}],
                             system=SYSTEM_PROMPT)

    resp1 = send_request(payload_a, model="team-smart-router")
    resp2 = send_request(payload_b, model="team-smart-router")

    return ("F", [resp1, resp2])


def test_g_different_model_router():
    """G: Complexity router picks different models — expected cache miss, but logical content should match."""
    print("\n=== Test G: Different Models from Router ===")
    # Simple vs complex query — should route to different tiers
    payload_a = make_payload([{"role": "user", "content": "What is 2+2?"}],
                             system=SYSTEM_PROMPT)
    payload_b = make_payload([{"role": "user", "content":
        "Design a distributed key-value store with consistent hashing, "
        "Raft consensus, write-ahead logging, and multi-zone replication. "
        "Include CAP theorem tradeoffs in your design."}],
        system=SYSTEM_PROMPT)

    resp1 = send_request(payload_a, model="team-smart-router")
    resp2 = send_request(payload_b, model="team-smart-router")

    return ("G", [resp1, resp2])


def test_h_session_id_header():
    """H: With and without x-headroom-session-id — hot zone should be identical."""
    print("\n=== Test H: Session ID Header ===")
    payload = make_payload(BASE_MESSAGES.copy())

    resp1 = send_request(payload)
    # Second call with explicit session-id (if Headroom reads it from request headers)
    resp2 = httpx.post(
        f"{GATEMID_URL}/v1/chat/completions",
        headers={**HEADERS, "x-headroom-session-id": "test-session-001"},
        json={
            "model": "gemini-flash",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + BASE_MESSAGES,
        },
        timeout=60,
    )
    resp2.raise_for_status()

    return ("H", [resp1, resp2.json()])


# ── Configuration Management ─────────────────────────────────────────────

def backup_config():
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, CONFIG_BACKUP_PATH)
        print(f"Backed up config to {CONFIG_BACKUP_PATH}")


def restore_config():
    if CONFIG_BACKUP_PATH.exists():
        shutil.copy2(CONFIG_BACKUP_PATH, CONFIG_PATH)
        CONFIG_BACKUP_PATH.unlink()
        print(f"Restored original config from backup")


def create_mock_config():
    """Create a test litellm_config.yaml that points at the mock server."""
    mock_config = {
        "model_list": [
            {
                "model_name": "gemini-flash",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_base": f"{MOCK_URL}/v1",
                    "api_key": "sk-mock-key",
                },
            },
            {
                "model_name": "gemini-pro",
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "api_base": f"{MOCK_URL}/v1",
                    "api_key": "sk-mock-key",
                },
            },
            {
                "model_name": "deepseek-flash",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_base": f"{MOCK_URL}/v1",
                    "api_key": "sk-mock-key",
                },
            },
            {
                "model_name": "deepseek-pro",
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "api_base": f"{MOCK_URL}/v1",
                    "api_key": "sk-mock-key",
                },
            },
            {
                "model_name": "team-smart-router",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_base": f"{MOCK_URL}/v1",
                    "api_key": "sk-mock-key",
                },
            },
        ],
        "litellm_settings": {
            "drop_params": True,
            "num_retries": 1,
            "request_timeout": 30,
        },
        "router_settings": {
            "routing_strategy": "simple-shuffle",
        },
        "general_settings": {
            "master_key": GATEWAY_KEY,
        },
    }
    return mock_config


def deploy_test_config():
    """Write test config and restart the litellm container."""
    mock_config = create_mock_config()
    CONFIG_PATH.write_text(yaml.dump(mock_config, default_flow_style=False))
    print("Deployed test config pointing at mock server")

    # Restart container
    result = subprocess.run(
        ["docker", "compose", "restart", "litellm"],
        capture_output=True, text=True, timeout=60,
    )
    print(f"Container restart: {result.stdout.strip() or 'ok'}")
    time.sleep(5)  # Wait for health check


def wait_for_gatemid(timeout: int = 30):
    """Wait for GateMid to be healthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(
                f"{GATEMID_URL}/health",
                headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
                timeout=5,
            )
            if resp.status_code == 200:
                print("GateMid is healthy")
                return True
        except Exception:
            pass
        time.sleep(2)
    print("ERROR: GateMid did not become healthy")
    return False


# ── Capture Analysis ─────────────────────────────────────────────────────

def read_captures() -> list[dict]:
    """Read all capture files from the captures directory."""
    captures = []
    for f in sorted(CAPTURES_DIR.glob("capture_*.json")):
        captures.append(json.loads(f.read_text()))
    # Sort by capture_id
    captures.sort(key=lambda c: c.get("capture_id", 0))
    return captures


def analyze_captures():
    """Analyze captured payloads and compare hot zones across test pairs."""
    captures = read_captures()
    print(f"\n{'='*60}")
    print(f"Analyzing {len(captures)} captured requests")
    print(f"{'='*60}")

    if not captures:
        print("No captures found. Did the mock server receive any requests?")
        return

    # Group captures by model + content hash for comparison
    # For now, compare sequential pairs (each test sends 2 requests)
    for i in range(0, len(captures) - 1, 2):
        if i + 1 >= len(captures):
            break
        a = captures[i]
        b = captures[i + 1]
        test_name = f"Pair {i//2}"
        print(f"\n--- Capture {a['capture_id']} vs {b['capture_id']} ---")
        print(f"  Model A: {a.get('model', '?')}  Model B: {b.get('model', '?')}")
        print(f"  Body size A: {a['body_bytes']} bytes  Body size B: {b['body_bytes']} bytes")
        diff_report(test_name, a["body"], b["body"])

    # Also compare all pairs for tests A/B/C/D/F/H where hot zone should be same
    print(f"\n{'='*60}")
    print("Cross-pair analysis (all captures with same model)")
    print(f"{'='*60}")

    by_model: dict[str, list[dict]] = {}
    for c in captures:
        model = c.get("model", "unknown")
        by_model.setdefault(model, []).append(c)

    for model, group in by_model.items():
        if len(group) < 2:
            continue
        print(f"\n  Model group: {model} ({len(group)} captures)")
        baseline = group[0]
        for j in range(1, len(group)):
            test_label = f"{model}:capture{group[j]['capture_id']}"
            diff_report(test_label, baseline["body"], group[j]["body"])


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Cache stability test harness")
    parser.add_argument("--deploy", action="store_true",
                        help="Deploy mock config and restart GateMid")
    parser.add_argument("--restore", action="store_true",
                        help="Restore original config")
    parser.add_argument("--analyze", action="store_true",
                        help="Analyze existing captures")
    parser.add_argument("--run-tests", action="store_true",
                        help="Run all test cases (requires mock server already running)")
    args = parser.parse_args()

    if args.deploy:
        backup_config()
        deploy_test_config()
        wait_for_gatemid()

    if args.restore:
        restore_config()
        subprocess.run(["docker", "compose", "restart", "litellm"], timeout=60)
        time.sleep(5)

    if args.analyze:
        analyze_captures()
        return

    if args.run_tests:
        if not wait_for_gatemid():
            sys.exit(1)

        # Clear old captures
        for f in CAPTURES_DIR.glob("capture_*.json"):
            f.unlink()

        tests = [
            test_a_baseline_repeat,
            test_b_new_user_turn,
            test_c_large_tool_output,
            test_d_timestamp_same_day,
            test_e_timestamp_different_day,
            test_f_same_model_router,
            test_g_different_model_router,
            test_h_session_id_header,
        ]

        for test_fn in tests:
            try:
                name, _ = test_fn()
                print(f"  ✓ Test {name} completed")
            except Exception as e:
                print(f"  ✗ Test {name} FAILED: {e}", file=sys.stderr)

        print(f"\nAll tests completed. {len(list(CAPTURES_DIR.glob('capture_*.json')))} captures saved.")
        analyze_captures()
        return

    # Default: run full workflow
    print("=" * 60)
    print("Cache Stability Verification (Part 1)")
    print("=" * 60)
    print()
    print("Prerequisites:")
    print("  1. python tests/verify_cache_mock_server.py  (in another terminal)")
    print("  2. This script will modify litellm_config.yaml temporarily")
    print()
    print("Run with flags:")
    print("  --run-tests    Run all test cases (mock server must already be running)")
    print("  --deploy       Deploy mock config and restart GateMid")
    print("  --restore      Restore original config")
    print("  --analyze      Analyze existing captures")
    print()
    print("Full workflow:")
    print("  python tests/verify_cache_stability.py --deploy --run-tests --analyze")
    print()

    if input("Deploy test config and run tests? [y/N] ").lower().startswith("y"):
        backup_config()
        deploy_test_config()
        if wait_for_gatemid():
            tests = [
                test_a_baseline_repeat,
                test_b_new_user_turn,
                test_c_large_tool_output,
                test_d_timestamp_same_day,
                test_e_timestamp_different_day,
                test_f_same_model_router,
                test_g_different_model_router,
                test_h_session_id_header,
            ]
            for test_fn in tests:
                try:
                    name, _ = test_fn()
                    print(f"  ✓ Test {name} completed")
                except Exception as e:
                    print(f"  ✗ Test {name} FAILED: {e}", file=sys.stderr)

        restore_config()
        subprocess.run(["docker", "compose", "restart", "litellm"], timeout=60)

        print(f"\nTest results:")
        analyze_captures()


if __name__ == "__main__":
    main()
