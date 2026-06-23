"""Tests for the Redis data store layer (eval/redis_store.py).

Uses mocking for the Redis client — no real Redis instance needed.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from eval.redis_store import (
    enqueue_call_record,
    dequeue_call_record,
    queue_length,
    write_scored_call,
    get_worst_calls,
    get_best_calls,
)


# ── Queue helpers ─────────────────────────────────────────────────────────────

class TestQueueHelpers:
    def test_enqueue_call_record(self):
        mock_r = MagicMock()
        record = {"call_id": "abc", "question": "Hello"}
        with patch("eval.redis_store.r", mock_r):
            enqueue_call_record(record)
        mock_r.lpush.assert_called_once_with(
            "eval:pending", json.dumps(record)
        )

    def test_dequeue_call_record(self):
        mock_r = MagicMock()
        mock_r.brpop.return_value = ("eval:pending", json.dumps({"call_id": "abc"}))
        with patch("eval.redis_store.r", mock_r):
            result = dequeue_call_record(timeout=5)
        assert result == {"call_id": "abc"}
        mock_r.brpop.assert_called_once_with("eval:pending", timeout=5)

    def test_dequeue_timeout_returns_none(self):
        mock_r = MagicMock()
        mock_r.brpop.return_value = None
        with patch("eval.redis_store.r", mock_r):
            result = dequeue_call_record(timeout=5)
        assert result is None

    def test_queue_length(self):
        mock_r = MagicMock()
        mock_r.llen.return_value = 3
        with patch("eval.redis_store.r", mock_r):
            result = queue_length()
        assert result == 3


# ── write_scored_call ─────────────────────────────────────────────────────────

class TestWriteScoredCall:
    @pytest.fixture
    def scored_record(self):
        return {
            "call_id": "test-001",
            "timestamp": "2026-06-01T12:00:00",
            "question": "What is FHIR?",
            "answer": "FHIR is a standard for healthcare data exchange.",
            "composite_score": 0.8734,
            "scores": {
                "faithfulness": 0.95,
                "answer_relevancy": 0.88,
                "context_precision": 0.72,
                "context_recall": None,
            },
            "model": "gemini-flash",
            "tokens_in": 100,
            "tokens_out": 50,
            "request_category": "fhir_query",
            "prompt_id": "v2_prompt",
        }

    def test_writes_hash(self, scored_record):
        mock_r = MagicMock()
        with patch("eval.redis_store.r", mock_r):
            write_scored_call(scored_record)

        # Should have called hset with eval:call:test-001
        hset_calls = [c for c in mock_r.method_calls if c[0] == "hset"]
        assert len(hset_calls) >= 1
        name, args, kwargs = hset_calls[0]
        assert "eval:call:test-001" in args or (
            kwargs.get("name") == "eval:call:test-001"
        )

    def test_sets_ttl(self, scored_record):
        mock_r = MagicMock()
        with patch("eval.redis_store.r", mock_r):
            write_scored_call(scored_record)

        expire_calls = [c for c in mock_r.method_calls if c[0] == "expire"]
        assert len(expire_calls) >= 1

    def test_writes_sorted_sets(self, scored_record):
        mock_r = MagicMock()
        with patch("eval.redis_store.r", mock_r):
            write_scored_call(scored_record)

        zadd_calls = [c for c in mock_r.method_calls if c[0] == "zadd"]
        keys = [c.args[0] for c in zadd_calls]
        assert "eval:scores:all" in keys
        assert "eval:scores:cat:fhir_query" in keys
        assert "eval:scores:prompt:v2_prompt" in keys

    def test_updates_stats(self, scored_record):
        mock_r = MagicMock()
        with patch("eval.redis_store.r", mock_r):
            write_scored_call(scored_record)

        mock_r.hincrby.assert_any_call("eval:meta:stats", "total_scored", 1)
        mock_r.hset.assert_any_call(
            "eval:meta:stats", "last_scored_at", scored_record["timestamp"]
        )


# ── Read helpers ──────────────────────────────────────────────────────────────

class TestReadHelpers:
    def test_get_worst_calls(self):
        mock_r = MagicMock()
        mock_r.zrange.return_value = ["test-001", "test-002"]
        mock_r.exists.return_value = True
        mock_r.hgetall.return_value = {
            "call_id": "test-001",
            "composite_score": "0.25",
            "scores_json": json.dumps({"faithfulness": 0.3}),
            "question": "test",
            "answer": "ans",
            "request_category": "general",
            "model": "m",
            "tokens_in": "10",
            "tokens_out": "5",
            "prompt_id": "default",
            "timestamp": "now",
        }
        with patch("eval.redis_store.r", mock_r):
            results = get_worst_calls(n=2)

        assert len(results) == 2
        mock_r.zrange.assert_called_once()
        # ascending = lowest scores first
        assert mock_r.zrange.call_args[0][2] == 1

    def test_get_best_calls(self):
        mock_r = MagicMock()
        mock_r.zrevrange.return_value = ["best-001"]
        mock_r.exists.return_value = True
        mock_r.hgetall.return_value = {
            "call_id": "best-001",
            "composite_score": "0.95",
            "scores_json": json.dumps({"faithfulness": 0.95}),
            "question": "best",
            "answer": "ans",
            "request_category": "general",
            "model": "m",
            "tokens_in": "10",
            "tokens_out": "5",
            "prompt_id": "default",
            "timestamp": "now",
        }
        with patch("eval.redis_store.r", mock_r):
            results = get_best_calls(n=1)

        assert len(results) == 1
        # best uses zrevrange (descending)
        assert len(mock_r.zrevrange.call_args_list) == 1

    def test_get_worst_filters_missing(self):
        """Calls whose hash has expired should be filtered out."""
        mock_r = MagicMock()
        mock_r.zrange.return_value = ["dead-001"]
        mock_r.exists.return_value = False
        with patch("eval.redis_store.r", mock_r):
            results = get_worst_calls(n=5)
        assert len(results) == 0

    def test_hydrate_parses_scores_json(self):
        mock_r = MagicMock()
        mock_r.zrevrange.return_value = ["test-001"]
        mock_r.exists.return_value = True
        mock_r.hgetall.return_value = {
            "call_id": "test-001",
            "composite_score": "0.80",
            "scores_json": json.dumps({"faithfulness": 0.80}),
            "question": "q",
            "answer": "a",
            "request_category": "general",
            "model": "m",
            "tokens_in": "10",
            "tokens_out": "5",
            "prompt_id": "default",
            "timestamp": "now",
        }
        with patch("eval.redis_store.r", mock_r):
            results = get_best_calls(n=1)
        assert results[0]["scores"] == {"faithfulness": 0.80}
        assert "scores_json" not in results[0]

    def test_category_filter(self):
        mock_r = MagicMock()
        mock_r.zrange.return_value = []
        with patch("eval.redis_store.r", mock_r):
            get_worst_calls(n=10, category="hl7_transform")
        # Should use the category-specific key
        assert "eval:scores:cat:hl7_transform" in mock_r.zrange.call_args[0]

    def test_prompt_id_filter(self):
        mock_r = MagicMock()
        mock_r.zrevrange.return_value = []
        with patch("eval.redis_store.r", mock_r):
            get_best_calls(n=10, prompt_id="v2_prompt")
        # Should use the prompt-specific key (priority over category)
        assert "eval:scores:prompt:v2_prompt" in mock_r.zrevrange.call_args[0]

    def test_prompt_id_overrides_category(self):
        """When both prompt_id and category are set, prompt_id wins."""
        mock_r = MagicMock()
        mock_r.zrange.return_value = []
        with patch("eval.redis_store.r", mock_r):
            get_worst_calls(n=10, category="fhir_query", prompt_id="v2_prompt")
        key = mock_r.zrange.call_args[0][0]
        assert "prompt:v2_prompt" in key
        assert "cat:" not in key
