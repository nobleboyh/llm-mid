"""Unit tests for session fingerprint and switch-log helpers."""
from __future__ import annotations

import datetime

import pytest

from proxy.callback import (
    _session_fingerprint,
    _first_system_content,
    _first_user_content,
    _compute_seconds_since,
)


class TestSessionFingerprint:
    def test_method_a_header_session_id(self):
        """Method A picks up x-headroom-session-id header."""
        kwargs = {
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {"x-headroom-session-id": "abc-123"},
                    "body": {"messages": [
                        {"role": "system", "content": "You are helpful."},
                        {"role": "user", "content": "Hello"},
                    ]},
                }
            }
        }
        assert _session_fingerprint(kwargs) == "header:abc-123"

    def test_method_a_header_session_id_alt(self):
        """Method A picks up x-session-id header."""
        kwargs = {
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {"x-session-id": "sess-456"},
                    "body": {"messages": []},
                }
            }
        }
        assert _session_fingerprint(kwargs) == "header:sess-456"

    def test_method_a_header_conversation_id(self):
        """Method A picks up x-conversation-id header."""
        kwargs = {
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {"x-conversation-id": "conv-789"},
                    "body": {"messages": []},
                }
            }
        }
        assert _session_fingerprint(kwargs) == "header:conv-789"

    def test_method_a_header_priority(self):
        """First found header wins (x-headroom-session-id checked first)."""
        kwargs = {
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {
                        "x-session-id": "second",
                        "x-headroom-session-id": "first",
                    },
                    "body": {"messages": []},
                }
            }
        }
        assert _session_fingerprint(kwargs) == "header:first"

    def test_method_b_string_content(self):
        """Method B falls back to structural fingerprint (string content)."""
        kwargs = {
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {},
                    "body": {
                        "messages": [
                            {"role": "system", "content": "You are a helpful assistant."},
                            {"role": "user", "content": "What is the weather?"},
                        ],
                        "tools": [],
                    },
                }
            }
        }
        result = _session_fingerprint(kwargs)
        assert result.startswith("fp:")
        assert len(result) == 19  # "fp:" + 16 hex chars

    def test_method_b_list_content(self):
        """Method B handles Anthropic list-of-blocks content format."""
        kwargs = {
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {},
                    "body": {
                        "messages": [
                            {"role": "system", "content": [{"type": "text", "text": "System prompt here"}]},
                            {"role": "user", "content": [{"type": "text", "text": "User message here"}]},
                        ],
                        "tools": [],
                    },
                }
            }
        }
        result = _session_fingerprint(kwargs)
        assert result.startswith("fp:")

    def test_method_b_deterministic(self):
        """Same messages produce the same fingerprint."""
        body = {
            "messages": [
                {"role": "system", "content": "Sys"},
                {"role": "user", "content": "Hi"},
            ],
            "tools": [],
        }
        kwargs1 = {"litellm_params": {"proxy_server_request": {"headers": {}, "body": body}}}
        kwargs2 = {"litellm_params": {"proxy_server_request": {"headers": {}, "body": body}}}
        assert _session_fingerprint(kwargs1) == _session_fingerprint(kwargs2)

    def test_method_b_different_first_user(self):
        """Different first user messages produce different fingerprints."""
        def make_kwargs(user_msg):
            return {
                "litellm_params": {
                    "proxy_server_request": {
                        "headers": {},
                        "body": {
                            "messages": [
                                {"role": "system", "content": "Sys"},
                                {"role": "user", "content": user_msg},
                            ],
                            "tools": [],
                        },
                    }
                }
            }
        assert _session_fingerprint(make_kwargs("Hello")) != _session_fingerprint(make_kwargs("Goodbye"))

    def test_method_b_different_tools(self):
        """Different tools produce different fingerprints."""
        def make_kwargs(tools):
            return {
                "litellm_params": {
                    "proxy_server_request": {
                        "headers": {},
                        "body": {
                            "messages": [
                                {"role": "system", "content": "Sys"},
                                {"role": "user", "content": "Hi"},
                            ],
                            "tools": tools,
                        },
                    }
                }
            }
        assert _session_fingerprint(make_kwargs(
            [{"name": "read_file"}]
        )) != _session_fingerprint(make_kwargs(
            [{"name": "write_file"}]
        ))

    def test_method_b_tool_order_deterministic(self):
        """Same tools in different order produce the same fingerprint (sorted)."""
        body = {
            "messages": [
                {"role": "system", "content": "Sys"},
                {"role": "user", "content": "Hi"},
            ],
        }
        k1 = {"litellm_params": {"proxy_server_request": {
            "headers": {}, "body": {**body, "tools": [{"name": "b"}, {"name": "a"}]}
        }}}
        k2 = {"litellm_params": {"proxy_server_request": {
            "headers": {}, "body": {**body, "tools": [{"name": "a"}, {"name": "b"}]}
        }}}
        assert _session_fingerprint(k1) == _session_fingerprint(k2)

    def test_method_b_empty_messages(self):
        """Empty messages list returns empty string."""
        kwargs = {
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {},
                    "body": {"messages": []},
                }
            }
        }
        assert _session_fingerprint(kwargs) == ""

    def test_method_b_missing_body(self):
        """Missing body returns empty string."""
        kwargs = {
            "litellm_params": {
                "proxy_server_request": {"headers": {}},
            }
        }
        assert _session_fingerprint(kwargs) == ""

    def test_method_b_missing_proxy_server_request(self):
        """Missing proxy_server_request returns empty string."""
        kwargs = {"litellm_params": {}}
        assert _session_fingerprint(kwargs) == ""


class TestContentExtraction:
    def test_first_system_content_string(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        assert _first_system_content(messages) == "You are helpful."

    def test_first_system_content_list(self):
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "System prompt"}]},
        ]
        assert _first_system_content(messages) == "System prompt"

    def test_first_system_content_list_multiple(self):
        messages = [
            {"role": "system", "content": [
                {"type": "text", "text": "Part A"},
                {"type": "text", "text": "Part B"},
            ]},
        ]
        assert "Part A" in _first_system_content(messages)
        assert "Part B" in _first_system_content(messages)

    def test_first_system_content_none(self):
        messages = [{"role": "user", "content": "Hi"}]
        assert _first_system_content(messages) == ""

    def test_first_user_content_string(self):
        messages = [
            {"role": "system", "content": "Sys"},
            {"role": "user", "content": "What is the time?"},
        ]
        assert _first_user_content(messages) == "What is the time?"

    def test_first_user_content_list(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "User question"}]},
        ]
        assert _first_user_content(messages) == "User question"

    def test_first_user_content_mixed_list(self):
        messages = [
            {"role": "user", "content": [
                {"type": "image", "source": {}},
                {"type": "text", "text": "Describe this image"},
            ]},
        ]
        assert "Describe this image" in _first_user_content(messages)


class TestComputeSecondsSince:
    def test_gap(self):
        last = "2026-07-15T14:00:00+00:00"
        now = datetime.datetime(2026, 7, 15, 14, 2, 30, tzinfo=datetime.timezone.utc)
        assert _compute_seconds_since(last, now) == 150.0

    def test_none_last_ts(self):
        now = datetime.datetime(2026, 7, 15, 14, 0, 0, tzinfo=datetime.timezone.utc)
        assert _compute_seconds_since(None, now) is None

    def test_garbled_last_ts(self):
        now = datetime.datetime(2026, 7, 15, 14, 0, 0, tzinfo=datetime.timezone.utc)
        assert _compute_seconds_since("not-a-timestamp", now) is None
