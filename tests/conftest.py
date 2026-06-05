"""Shared fixtures for GateMid integration tests."""
import os
import pytest
import subprocess
import time
import httpx


@pytest.fixture(scope="session")
def gateway_url():
    """Return the gateway base URL. Assumes docker compose up is running."""
    return os.environ.get("GATEMID_URL", "http://localhost:4000")


@pytest.fixture(scope="session")
def gateway_ready(gateway_url):
    """Wait for the gateway to be healthy before running tests."""
    client = httpx.Client(timeout=5.0)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            resp = client.get(f"{gateway_url}/health")
            if resp.status_code == 200:
                return True
        except httpx.ConnectError:
            pass
        time.sleep(1)
    pytest.fail(f"Gateway at {gateway_url} did not become healthy within 30s")
