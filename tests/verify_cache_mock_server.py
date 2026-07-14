#!/usr/bin/env python3
"""Transparent forwarding proxy — captures outbound request bytes, forwards to the real provider.

This is the "middle ground" approach from verify-001.md:
- Captures the exact bytes Headroom/GateMid sends upstream
- Forwards each request to the real provider API unmodified
- Returns the real response — live sessions and streaming work normally
- Zero risk: nothing is rerouted or replaced, just logged in transit

Usage (single terminal, separate from your live GateMid):

    # 1. Create a throwaway test config that points at this proxy:
    cp litellm_config.yaml litellm_config.test.yaml
    # edit all api_base urls: s/api\.anthropic\.com/localhost:18000/g
    #   s/api\.googleapis\.com/localhost:18000/g  etc.
    # or use the auto-generated one from verify_cache_stability.py

    # 2. Start this proxy:
    python tests/verify_cache_mock_server.py

    # 3. Start GateMid with the test config, on a different port:
    docker compose -f docker-compose.test.yml up -d    # OR:
    # Copy docker-compose.yml → docker-compose.test.yml, change port and config path

    # 4. Run tests against the test GateMid:
    GATEMID_URL=http://localhost:8788 python tests/verify_cache_stability.py --run-tests

    # 5. Analyze captures:
    python tests/verify_cache_stability.py --analyze

Ponytail: this is a single-file forwarding proxy. No config, no routing tables.
Each provider endpoint (gemini, anthropic, openai) just needs its base URL
mapped in UPSTREAM_MAP below. Currently maps:
  - /v1/chat/completions  → api.openai.com
  - /v1/messages          → api.anthropic.com
  - /v1beta               → generativeai.googleapis.com (vertex)
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("capture-proxy")

app = FastAPI()

CAPTURES_DIR = Path("./captures")
CAPTURES_DIR.mkdir(exist_ok=True)

capture_counter: int = 0

# ponytail: single upstream (DeepSeek), only key we have.
UPSTREAM_BASE = "https://api.deepseek.com"
DEEPSEEK_AUTH = f"Bearer {os.environ.get('DEEPSEEK_API_KEY', '')}"

# Shared httpx client (connection pooling, no limit for short test runs)
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0),
            # ponytail: no SSL verify — avoids local cert issues with forwarding
            verify=False,
        )
    return _client


def _capture(
    body_bytes: bytes, headers: dict[str, str], path: str, model: str,
) -> Path:
    """Write a capture record to disk. Returns the file path."""
    global capture_counter
    capture_counter += 1

    ts = datetime.now(timezone.utc).strftime("%H%M%S%f")
    body_hash = hashlib.sha256(body_bytes).hexdigest()[:12]
    filename = f"capture_{capture_counter:03d}_{ts}_{path.replace('/','_')}_{model}_{body_hash}.json"
    filepath = CAPTURES_DIR / filename

    try:
        body_json = json.loads(body_bytes)
    except json.JSONDecodeError:
        body_json = {"raw_bytes": body_bytes.decode("utf-8", errors="replace")}

    record: dict[str, Any] = {
        "capture_id": capture_counter,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "model": model,
        "headers": {
            k: v for k, v in headers.items()
            if k.lower() not in ("authorization", "x-api-key")  # don't store provider keys
        },
        "body": body_json,
        "body_hash": body_hash,
        "body_bytes": len(body_bytes),
    }

    filepath.write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
    )
    logger.info(
        "[CAPTURE #%03d] %s model=%s %d bytes → %s",
        capture_counter, path, model, len(body_bytes), filename,
    )
    return filepath


def _find_upstream(path: str) -> str | None:
    """Return the provider base URL. Always DeepSeek."""
    return UPSTREAM_BASE


async def _forward(request: Request) -> Response:
    """Capture + forward to real provider. Returns the real upstream response."""
    body = await request.body()
    path = request.url.path
    upstream_base = _find_upstream(path)

    if not upstream_base:
        logger.warning("No upstream mapping for path=%s, returning 502", path)
        return Response(
            json.dumps({"error": f"no upstream for {path}"}),
            status_code=502, media_type="application/json",
        )

    # Extract model name from body for capture filename
    model = "unknown"
    try:
        j = json.loads(body)
        model = str(j.get("model", "unknown"))
    except Exception:
        pass

    # ── Capture the outbound bytes before forwarding ─────────────────────
    _capture(body, dict(request.headers), path, model)

    # ── Forward to real provider ─────────────────────────────────────────
    upstream_url = f"{upstream_base}{path}"
    query = request.url.query
    if query:
        upstream_url += f"?{query}"

    client = await _get_client()

    # Forward headers, drop Host/connection/auth (always inject real DeepSeek auth)
    fwd_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() not in ("host", "content-length", "connection", "authorization"):
            fwd_headers[k] = v
    fwd_headers["Authorization"] = DEEPSEEK_AUTH

    try:
        resp = await client.request(
            method=request.method,
            url=upstream_url,
            content=body,
            headers=fwd_headers,
        )
        logger.info(
            "[FORWARD] %s → %s | %s %d",
            path, upstream_base, model, resp.status_code,
        )
    except httpx.TimeoutException:
        logger.error("[FORWARD] timeout %s → %s", path, upstream_base)
        return Response(
            json.dumps({"error": "upstream timeout"}),
            status_code=504, media_type="application/json",
        )
    except Exception as e:
        logger.error("[FORWARD] error %s → %s: %s", path, upstream_base, e)
        return Response(
            json.dumps({"error": str(e)}),
            status_code=502, media_type="application/json",
        )

    # Return the real response to GateMid — streaming and all
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


# ── Catch-all route ─────────────────────────────────────────────────────


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, _path: str = "") -> Response:
    return await _forward(request)


# ── Standalone entry point ──────────────────────────────────────────────

if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", message="TLS certificate.*not verified")

    print("═" * 64)
    print("  GateMid Cache-Stability Capture Proxy")
    print("═" * 64)
    print()
    print(f"  Captures dir:  {CAPTURES_DIR.resolve()}")
    print(f"  Upstream:      {UPSTREAM_BASE}  (DeepSeek)")
    print()
    print("  Steps:")
    print("    1. python tests/verify_cache_mock_server.py     (this)")
    print("    2. python tests/verify_cache_stability.py --run-tests")
    print("    3. python tests/verify_cache_stability.py --analyze")
    print()
    print(f"  Listening on :18000")
    print("═" * 64)
    uvicorn.run(app, host="0.0.0.0", port=18000, log_level="info")
