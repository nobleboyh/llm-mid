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


import json
from unittest.mock import MagicMock, patch

from proxy.callback import SESSION_TTL, SESSION_DAYS_KEY


class TestLogSessionSwitch:
    """Test _log_session_switch via mocking Redis."""

    def make_kwargs(self, model="deepseek-pro", session_header=None, messages=None):
        """Build kwargs dict matching what LiteLLM passes to the callback."""
        if messages is None:
            messages = [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ]
        headers = {}
        if session_header:
            headers["x-headroom-session-id"] = session_header
        return {
            "model": model,
            "litellm_params": {
                "proxy_server_request": {
                    "headers": headers,
                    "body": {"messages": messages, "tools": []},
                },
            },
        }

    def test_first_request_creates_session(self):
        """First request for a session creates list, meta, and days index."""
        mock_redis = MagicMock()
        mock_redis.hget.return_value = None  # no previous state

        with patch("proxy.callback.hot_zone_token_var") as mock_cv, \
             patch("eval.redis_store.r", mock_redis):
            mock_cv.get.return_value = 3400
            from proxy.callback import RagasLogger
            logger = RagasLogger()
            logger._log_session_switch(self.make_kwargs(session_header="sess-1"))

        # LPUSH called
        mock_redis.lpush.assert_called_once()
        list_key = mock_redis.lpush.call_args[0][0]
        assert list_key.startswith("router:session:header:")
        mock_redis.expire.assert_any_call(list_key, SESSION_TTL)

        # Event JSON pushed
        event = json.loads(mock_redis.lpush.call_args[0][1])
        assert event["model"] == "deepseek-pro"
        assert event["previous_model"] is None
        assert event["seconds_since_last"] is None
        assert event["hot_zone_tokens"] == 3400

        # Meta hash set
        meta_key = f"{list_key}:meta"
        mock_redis.hset.assert_any_call(meta_key, mapping={
            "latest_model": "deepseek-pro",
            "latest_timestamp": event["timestamp"],
        })
        mock_redis.expire.assert_any_call(meta_key, SESSION_TTL)

        # Days index (first request only)
        mock_redis.zadd.assert_called_once()

    def test_second_request_detects_switch(self):
        """Second request with different model detects switch."""
        mock_redis = MagicMock()
        # Simulate previous state
        mock_redis.hget.side_effect = lambda key, field: {
            ("router:session:header:sess-1:meta", "latest_model"): "gemini-flash",
            ("router:session:header:sess-1:meta", "latest_timestamp"): "2026-07-15T14:00:00+00:00",
        }.get((key, field))

        with patch("proxy.callback.hot_zone_token_var") as mock_cv, \
             patch("eval.redis_store.r", mock_redis):
            mock_cv.get.return_value = 3400
            from proxy.callback import RagasLogger
            logger = RagasLogger()
            logger._log_session_switch(self.make_kwargs(
                model="deepseek-pro",
                session_header="sess-1",
            ))

        event = json.loads(mock_redis.lpush.call_args[0][1])
        assert event["model"] == "deepseek-pro"
        assert event["previous_model"] == "gemini-flash"
        assert event["seconds_since_last"] is not None  # gap computed
        assert event["hot_zone_tokens"] == 3400

        # Meta updated
        meta_key = "router:session:header:sess-1:meta"
        mock_redis.hset.assert_called_with(meta_key, mapping={
            "latest_model": "deepseek-pro",
            "latest_timestamp": event["timestamp"],
        })

        # Days index NOT called (not first request)
        mock_redis.zadd.assert_not_called()

    def test_same_model_no_switch(self):
        """Consecutive requests with same model show previous_model == current."""
        mock_redis = MagicMock()
        mock_redis.hget.side_effect = lambda key, field: {
            ("router:session:header:sess-1:meta", "latest_model"): "gemini-flash",
            ("router:session:header:sess-1:meta", "latest_timestamp"): "2026-07-15T14:00:00+00:00",
        }.get((key, field))

        with patch("proxy.callback.hot_zone_token_var") as mock_cv, \
             patch("eval.redis_store.r", mock_redis):
            mock_cv.get.return_value = 3400
            from proxy.callback import RagasLogger
            logger = RagasLogger()
            logger._log_session_switch(self.make_kwargs(model="gemini-flash", session_header="sess-1"))

        event = json.loads(mock_redis.lpush.call_args[0][1])
        assert event["model"] == "gemini-flash"
        assert event["previous_model"] == "gemini-flash"

    def test_redis_failure_is_silent(self):
        """Redis failure never raises."""
        mock_redis = MagicMock()
        mock_redis.hget.side_effect = ConnectionError("redis down")

        with patch("proxy.callback.hot_zone_token_var") as mock_cv, \
             patch("eval.redis_store.r", mock_redis):
            mock_cv.get.return_value = 0
            from proxy.callback import RagasLogger
            logger = RagasLogger()
            # Should not raise
            logger._log_session_switch(self.make_kwargs())

    def test_empty_fingerprint_skips(self):
        """Empty fingerprint returns early, no Redis calls."""
        mock_redis = MagicMock()

        with patch("proxy.callback.hot_zone_token_var") as mock_cv:
            mock_cv.get.return_value = 0
            from proxy.callback import RagasLogger
            logger = RagasLogger()
            # No messages -> fingerprint returns ""
            logger._log_session_switch({"litellm_params": {}})

        mock_redis.lpush.assert_not_called()
        mock_redis.hset.assert_not_called()

    def test_zero_hot_zone_tokens(self):
        """hot_zone_tokens is 0 when ContextVar default is returned."""
        mock_redis = MagicMock()
        mock_redis.hget.return_value = None

        with patch("proxy.callback.hot_zone_token_var") as mock_cv, \
             patch("eval.redis_store.r", mock_redis):
            mock_cv.get.return_value = 0
            from proxy.callback import RagasLogger
            logger = RagasLogger()
            logger._log_session_switch(self.make_kwargs())

        event = json.loads(mock_redis.lpush.call_args[0][1])
        assert event["hot_zone_tokens"] == 0
