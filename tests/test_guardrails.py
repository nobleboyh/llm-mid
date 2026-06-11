"""Tests for the API key masking guardrail.

Covers:
- Unit tests for ``mask_api_keys_in_text`` (each pattern)
- Unit tests for ``mask_api_keys_in_json`` (nested structures)
- Integration test via the live proxy (needs container running)
"""

from __future__ import annotations

import os

import pytest
import requests

from guardrails.api_key_masking import (
    mask_api_keys_in_json,
    mask_api_keys_in_text,
)

# ── Unit tests: mask_api_keys_in_text ─────────────────────────────────────────


def test_openai_key() -> None:
    text = "My key is sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    masked, events = mask_api_keys_in_text(text)
    assert "sk-proj-***MASKED***" in masked, f"got: {masked!r}"
    assert masked != text
    assert any(e["key_type"] == "openai_key" for e in events), events


def test_gemini_key() -> None:
    text = "Using AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz1234567890"
    masked, events = mask_api_keys_in_text(text)
    assert "AIzaSy***MASKED***" in masked
    assert any(e["key_type"] == "gemini_key" for e in events)


def test_bearer_token() -> None:
    text = 'Authorization: Bearer abCdEfGhIjKlMnOpQrStUvWxYz1234567890'
    masked, events = mask_api_keys_in_text(text)
    assert "Bearer ***MASKED***" in masked, f"got: {masked!r}"
    assert any(e["key_type"] == "bearer_token" for e in events), events


def test_huggingface_token() -> None:
    text = "token is hf_XyZaLmnOpQrStUvWxYzAbCdEfGh1234567890"
    masked, events = mask_api_keys_in_text(text)
    assert "hf_***MASKED***" in masked, f"got: {masked!r}"
    assert any(e["key_type"] == "huggingface_token" for e in events), events


def test_github_token() -> None:
    text = "ghs_AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGh1234"
    masked, events = mask_api_keys_in_text(text)
    assert "ghs_***MASKED***" in masked, f"got: {masked!r}"
    assert any(e["key_type"] == "github_token" for e in events), events


def test_aws_access_key() -> None:
    text = "AKIAQWRASDFGHJKLZXCV"
    masked, events = mask_api_keys_in_text(text)
    assert "AKIA***MASKED***" in masked, f"got: {masked!r}"
    assert any(e["key_type"] == "aws_access_key" for e in events), events


def test_api_key_value_pattern() -> None:
    """Only the ``api_key_value`` pattern should fire (key has no known prefix)."""
    text = 'The config has api_key=3Cr3tK3y1234567890123456789'
    masked, events = mask_api_keys_in_text(text)
    assert "***MASKED***" in masked, f"got: {masked!r}"
    assert any(e["key_type"] == "api_key_value" for e in events), events


def test_no_matching_key() -> None:
    text = "This is a normal sentence with no API keys."
    masked, events = mask_api_keys_in_text(text)
    assert masked == text
    assert events == []


def test_empty_string() -> None:
    masked, events = mask_api_keys_in_text("")
    assert masked == ""
    assert events == []


def test_non_string_passthrough() -> None:
    masked, events = mask_api_keys_in_text(None)  # type: ignore[arg-type]
    assert masked is None
    assert events == []


def test_multiple_keys_in_one_text() -> None:
    text = (
        "OpenAI: sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz. "
        "Gemini: AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz1234567890."
    )
    masked, events = mask_api_keys_in_text(text)
    assert "sk-proj-***MASKED***" in masked, f"got: {masked!r}"
    assert "AIzaSy***MASKED***" in masked
    key_types = {e["key_type"] for e in events}
    assert "openai_key" in key_types, f"events: {events}"
    assert "gemini_key" in key_types, f"events: {events}"


# ── Unit tests: mask_api_keys_in_json ─────────────────────────────────────────


def test_json_simple_string() -> None:
    data = {"content": "My key is sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456"}
    masked, events = mask_api_keys_in_json(data)
    assert "***MASKED***" in masked["content"]
    assert any(e["path"] == "$.content" for e in events)


def test_json_nested_structure() -> None:
    data = {
        "messages": [
            {"role": "system", "content": "API key: AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz1234567890"},
            {"role": "user", "content": "Key is sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456"},
        ],
        "model": "gemini-pro",
    }
    masked, events = mask_api_keys_in_json(data)
    assert "AIzaSy***MASKED***" in masked["messages"][0]["content"], masked["messages"][0]
    assert "sk-proj-***MASKED***" in masked["messages"][1]["content"], masked["messages"][1]
    assert masked["model"] == "gemini-pro"  # unchanged
    assert len(events) >= 2


def test_json_list_of_strings() -> None:
    data = ["safe text", "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456", "also safe"]
    masked, events = mask_api_keys_in_json(data)
    assert masked[1] != data[1]
    assert "***MOCKED***" not in str(masked)  # ensure not "MOCKED" typo vs "MASKED"
    assert "***MASKED***" in masked[1]


def test_json_no_keys() -> None:
    data = {"role": "user", "content": "Hello, how are you?"}
    masked, events = mask_api_keys_in_json(data)
    assert masked == data
    assert events == []


def test_json_non_string_values() -> None:
    data = {
        "name": "test",
        "count": 42,
        "ratio": 3.14,
        "enabled": True,
        "tags": None,
    }
    masked, events = mask_api_keys_in_json(data)
    assert masked == data
    assert events == []


# ── Integration tests (require running proxy) ─────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("GATEWAY_MASTER_KEY"),
    reason="GATEWAY_MASTER_KEY not set — proxy not running",
)
class TestApiKeyMaskingIntegration:
    """End-to-end tests against the running GateMid proxy.

    These tests send requests with API keys in the message body and verify
    the proxy doesn't crash (functional check). Full verification that keys
    were masked before reaching the provider requires provider-side logs.
    """

    GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:4000")

    @pytest.fixture
    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {os.environ['GATEWAY_MASTER_KEY']}",
            "Content-Type": "application/json",
        }

    def test_request_with_key_passes_through(self, auth_headers: dict[str, str]) -> None:
        """Send a request with an API key in the user message.

        The proxy should accept it, mask the key, and return a normal response
        (or an appropriate error if no models are configured).
        """
        payload = {
            "model": "gemini-flash",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "What is the value of my OpenAI key? "
                        "It is sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
                    ),
                },
            ],
        }
        resp = requests.post(
            f"{self.GATEWAY_URL}/chat/completions",
            headers=auth_headers,
            json=payload,
            timeout=30,
        )
        # The proxy should not crash — we expect either success (200) or
        # a model-not-available error. A 500 would indicate a crash.
        assert resp.status_code != 500, (
            f"Proxy returned 500 — masking may have broken the request: "
            f"{resp.text[:500]}"
        )
