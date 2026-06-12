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
    def test_skips_and_returns_none(self, logger, minimal_response):
        kwargs = {"model": "ragas-eval", "metadata": {}}
        result = logger.log_success_event(kwargs, minimal_response, None, None)
        assert result is None

    @patch("eval.redis_store.enqueue_call_record")
    def test_enqueues_normal_call(self, mock_enqueue, logger, minimal_response):
        kwargs = {
            "model": "gemini-flash",
            "metadata": {
                "request_category": "fhir_query",
                "prompt_id": "v2_prompt",
                "retrieved_context": ["Patient record 123"],
            },
            "messages": [
                {"role": "system", "content": "You are a FHIR assistant"},
                {"role": "user", "content": "What is the FHIR resource for Patient?"},
            ],
        }
        logger.log_success_event(kwargs, minimal_response, None, None)
        mock_enqueue.assert_called_once()
        record = mock_enqueue.call_args[0][0]
        assert record["question"] == "What is the FHIR resource for Patient?"
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
    def test_uses_last_user_message(self, mock_enqueue, logger, minimal_response):
        kwargs = {
            "model": "deepseek-flash",
            "metadata": {},
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
            ],
        }
        logger.log_success_event(kwargs, minimal_response, None, None)
        record = mock_enqueue.call_args[0][0]
        assert record["question"] == "Second question"

    @patch("eval.redis_store.enqueue_call_record")
    def test_empty_messages_falls_back(self, mock_enqueue, logger, minimal_response):
        kwargs = {"model": "gemini-flash", "metadata": {}, "messages": []}
        logger.log_success_event(kwargs, minimal_response, None, None)
        record = mock_enqueue.call_args[0][0]
        assert record["question"] == ""

    @patch("eval.redis_store.enqueue_call_record")
    def test_multimodal_content_joins_text(self, mock_enqueue, logger, minimal_response):
        kwargs = {
            "model": "gemini-flash",
            "metadata": {},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {"type": "image_url", "image_url": {"url": "data:image/..."}},
                ],
            }],
        }
        logger.log_success_event(kwargs, minimal_response, None, None)
        record = mock_enqueue.call_args[0][0]
        assert record["question"] == "What is in this image?"

    @patch("eval.redis_store.enqueue_call_record")
    def test_call_id_is_uuid(self, mock_enqueue, logger, minimal_response):
        import uuid
        kwargs = {"model": "gemini-flash", "metadata": {}, "messages": [
            {"role": "user", "content": "Hello"},
        ]}
        logger.log_success_event(kwargs, minimal_response, None, None)
        record = mock_enqueue.call_args[0][0]
        uuid.UUID(record["call_id"])  # raises ValueError if invalid

    @patch("eval.redis_store.enqueue_call_record")
    def test_enqueue_exception_caught(self, mock_enqueue, logger, minimal_response):
        mock_enqueue.side_effect = ConnectionError("Redis down")
        kwargs = {"model": "gemini-flash", "metadata": {}, "messages": [
            {"role": "user", "content": "Hello"},
        ]}
        # Should not raise
        logger.log_success_event(kwargs, minimal_response, None, None)
