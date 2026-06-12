"""Tests for the RagasLogger LiteLLM custom callback."""
from unittest.mock import patch, MagicMock

import pytest

from proxy.callback import RagasLogger


@pytest.fixture
def logger():
    return RagasLogger()


@pytest.fixture
def minimal_response():
    """Build a minimal LiteLLM response object."""
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = "The capital of France is Paris."
    choice.delta = None
    resp.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 50
    usage.completion_tokens = 20
    resp.usage = usage
    return resp


# ── helpers ──────────────────────────────────────────────────────────────────


def _standard_kwargs(**overrides) -> dict:
    """Minimal kwargs that pass the original_question check and enqueue."""
    base = {
        "model": "gemini-flash",
        "metadata": {
            "original_question": "What is the capital of France?",
            "request_category": "fhir_query",
            "prompt_id": "v2_prompt",
            "retrieved_context": ["Patient record 123"],
        },
        "messages": [],  # no longer used by callback, but kept to match reality
    }
    base.update(overrides)
    return base


# ── _should_skip ──────────────────────────────────────────────────────────────

class TestShouldSkip:
    def test_skips_ragas_eval_model(self, logger):
        kwargs = {"model": "ragas-eval"}
        assert logger._should_skip(kwargs) is True

    def test_skips_ragas_eval_with_version(self, logger):
        kwargs = {"model": "ragas-eval/gpt-4o-mini"}
        assert logger._should_skip(kwargs) is True

    def test_skips_metadata_flag(self, logger):
        kwargs = {"metadata": {"_ragas_eval_call": True}}
        assert logger._should_skip(kwargs) is True

    def test_does_not_skip_normal_call(self, logger):
        kwargs = {"model": "gemini-flash", "metadata": {}}
        assert logger._should_skip(kwargs) is False

    def test_does_not_skip_other_models(self, logger):
        kwargs = {"model": "deepseek-flash"}
        assert logger._should_skip(kwargs) is False

    def test_skips_model_with_prefix(self, logger):
        kwargs = {"model": "ragas-eval-prod"}
        assert logger._should_skip(kwargs) is True


# ── log_success_event ─────────────────────────────────────────────────────────

class TestLogSuccessEvent:
    def test_skips_ragas_eval_model(self, logger, minimal_response):
        kwargs = {"model": "ragas-eval", "metadata": {}}
        result = logger.log_success_event(kwargs, minimal_response, None, None)
        assert result is None

    @patch("eval.redis_store.enqueue_call_record")
    def test_skips_on_missing_question(self, mock_enqueue, logger, minimal_response):
        """No original_question in metadata → skip."""
        kwargs = {"model": "gemini-flash", "metadata": {}}
        logger.log_success_event(kwargs, minimal_response, None, None)
        mock_enqueue.assert_not_called()

    @patch("eval.redis_store.enqueue_call_record")
    def test_skips_on_whitespace_question(self, mock_enqueue, logger, minimal_response):
        """Whitespace-only original_question → skip."""
        kwargs = {"model": "gemini-flash", "metadata": {"original_question": "  "}}
        logger.log_success_event(kwargs, minimal_response, None, None)
        mock_enqueue.assert_not_called()

    @patch("eval.redis_store.enqueue_call_record")
    def test_enqueues_normal_call(self, mock_enqueue, logger, minimal_response):
        kwargs = _standard_kwargs()
        logger.log_success_event(kwargs, minimal_response, None, None)
        mock_enqueue.assert_called_once()
        record = mock_enqueue.call_args[0][0]
        assert record["question"] == "What is the capital of France?"
        assert "capital of France" in record["answer"]
        assert record["request_category"] == "fhir_query"
        assert record["prompt_id"] == "v2_prompt"
        assert record["contexts"] == ["Patient record 123"]
        assert record["model"] == "gemini-flash"
        assert record["tokens_in"] == 50
        assert record["tokens_out"] == 20
        assert "call_id" in record
        assert "timestamp" in record

    @patch("eval.redis_store.enqueue_call_record")
    def test_uses_original_question(self, mock_enqueue, logger, minimal_response):
        """original_question is used verbatim, not re-extracted from messages."""
        kwargs = _standard_kwargs(
            metadata={"original_question": "Pre-compression question here"},
            messages=[{"role": "user", "content": "Compressed/transformed text"}],
        )
        logger.log_success_event(kwargs, minimal_response, None, None)
        record = mock_enqueue.call_args[0][0]
        assert record["question"] == "Pre-compression question here"

    @patch("eval.redis_store.enqueue_call_record")
    def test_call_id_is_uuid(self, mock_enqueue, logger, minimal_response):
        import uuid
        kwargs = _standard_kwargs()
        logger.log_success_event(kwargs, minimal_response, None, None)
        record = mock_enqueue.call_args[0][0]
        uuid.UUID(record["call_id"])  # raises ValueError if invalid

    @patch("eval.redis_store.enqueue_call_record")
    def test_enqueue_exception_caught(self, mock_enqueue, logger, minimal_response):
        mock_enqueue.side_effect = ConnectionError("Redis down")
        kwargs = _standard_kwargs()
        # Should not raise
        logger.log_success_event(kwargs, minimal_response, None, None)
