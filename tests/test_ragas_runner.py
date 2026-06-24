"""Tests for the Ragas scoring logic (eval/worker.py).

Uses mocking for the Ragas evaluate() call — we test the composite scoring
logic, edge cases for missing contexts / ground_truth, and metric selection.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from eval.worker import compute_composite, score_record

# Pre-register mock ragas modules so the @patch("ragas.evaluate") class
# decorator doesn't trigger a real ragas import.  The real ragas package has
# a transitive import (langchain_community.chat_models.vertexai) that fails
# on CI when that package isn't installed at a compatible version.
# ponytail: global sys.modules mock; if worker.py stops using lazy ragas
# imports, switch to an explicit module-level mock fixture instead.
_ragas_evaluate_mock = MagicMock()
_ragas_mock = MagicMock()
_ragas_mock.evaluate = _ragas_evaluate_mock
sys.modules["ragas"] = _ragas_mock
sys.modules["ragas.metrics"] = MagicMock()

# ResponseRelevancy must be a real class, not a MagicMock instance —
# score_record() does isinstance(answer_relevancy, ResponseRelevancy),
# and isinstance second arg must be a type.
_mock_answer_relevance = MagicMock()
_mock_answer_relevance.ResponseRelevancy = type("ResponseRelevancy", (), {})
sys.modules["ragas.metrics._answer_relevance"] = _mock_answer_relevance


# ── compute_composite ─────────────────────────────────────────────────────────

class TestComputeComposite:
    def test_full_weights(self):
        scores = {"faithfulness": 0.8, "answer_relevancy": 0.7, "context_precision": 0.6}
        composite = compute_composite(scores, has_context=True)
        # 0.8*0.4 + 0.7*0.4 + 0.6*0.2 = 0.32 + 0.28 + 0.12 = 0.72
        assert composite == pytest.approx(0.72)

    def test_no_context_rebalance(self):
        scores = {"faithfulness": None, "answer_relevancy": 0.6, "context_precision": None}
        composite = compute_composite(scores, has_context=False)
        # faithfulness and context_precision excluded; only answer_relevancy at 1.0
        assert composite == pytest.approx(0.6)

    def test_all_perfect(self):
        scores = {"faithfulness": 1.0, "answer_relevancy": 1.0, "context_precision": 1.0}
        composite = compute_composite(scores, has_context=True)
        assert composite == pytest.approx(1.0)

    def test_all_zero(self):
        scores = {"faithfulness": 0.0, "answer_relevancy": 0.0, "context_precision": 0.0}
        composite = compute_composite(scores, has_context=True)
        assert composite == pytest.approx(0.0)

    def test_missing_metric_defaults_zero(self):
        scores = {"faithfulness": 0.5}
        composite = compute_composite(scores, has_context=True)
        # 0.5*0.4 + 0.0*0.4 + 0.0*0.2 = 0.20
        assert composite == pytest.approx(0.20)


# ── score_record (with mocked Ragas evaluate) ─────────────────────────────────

@pytest.fixture
def call_record():
    return {
        "call_id": "test-call-001",
        "question": "What is the capital of France?",
        "answer": "The capital of France is Paris.",
        "contexts": ["France is a country in Europe. Its capital is Paris."],
        "ground_truth": "",
        "request_category": "code_qa",
        "prompt_id": "default",
        "model": "gemini-flash",
        "tokens_in": 50,
        "tokens_out": 20,
    }


@pytest.fixture
def call_record_no_context():
    return {
        "call_id": "test-call-002",
        "question": "What is 2+2?",
        "answer": "4",
        "contexts": [],
        "ground_truth": "",
        "request_category": "general",
        "prompt_id": "default",
        "model": "deepseek-flash",
        "tokens_in": 10,
        "tokens_out": 5,
    }


class FakeRagasResult:
    """Mimics the Ragas evaluate() return type."""

    class FakePandas:
        class FakeIloc:
            def __getitem__(self, idx):
                return self

            def to_dict(self):
                return {
                    "faithfulness": 0.85,
                    "answer_relevancy": 0.75,
                    "context_precision": 0.65,
                    "context_recall": 0.0,
                }

        iloc = FakeIloc()

        def iloc_method(self, idx):
            return self

        def to_dict(self):
            return {
                "faithfulness": 0.85,
                "answer_relevancy": 0.75,
                "context_precision": 0.65,
                "context_recall": 0.0,
            }

    def to_pandas(self):
        return self.FakePandas()


@pytest.fixture
def fake_ragas():
    return FakeRagasResult()


@patch("ragas.evaluate", return_value=FakeRagasResult())
class TestScoreRecord:
    def test_enriches_record(self, mock_evaluate, call_record):
        result = score_record(call_record)
        assert "scores" in result
        assert "composite_score" in result

    def test_scores_rounded_to_4_places(self, mock_evaluate, call_record):
        result = score_record(call_record)
        for key, val in result["scores"].items():
            if val is not None:
                s = str(val)
                if "." in s:
                    assert len(s.split(".")[1]) <= 4

    def test_faithfulness_present(self, mock_evaluate, call_record):
        result = score_record(call_record)
        assert result["scores"]["faithfulness"] is not None

    def test_answer_relevancy_present(self, mock_evaluate, call_record):
        result = score_record(call_record)
        assert result["scores"]["answer_relevancy"] is not None

    def test_context_precision_empty(self, mock_evaluate, call_record_no_context):
        """When contexts is empty, context_precision and faithfulness should be None."""
        result = score_record(call_record_no_context)
        assert result["scores"]["context_precision"] is None
        assert result["scores"]["faithfulness"] is None

    def test_no_context_faithfulness_none(self, mock_evaluate, call_record_no_context):
        """When contexts is empty, faithfulness is skipped entirely."""
        result = score_record(call_record_no_context)
        assert result["scores"]["faithfulness"] is None
        # Composite should be driven by answer_relevancy alone
        assert result["composite_score"] == pytest.approx(0.75)  # 0.75 * 1.0

    def test_no_gt_no_recall_scored(self, mock_evaluate, call_record):
        result = score_record(call_record)
        assert result["scores"]["context_recall"] is None

    def test_with_ground_truth_shows_recall(self, mock_evaluate, call_record):
        """When ground_truth is present, context_recall should be scored."""
        call_record["ground_truth"] = "Paris is the capital of France."
        result = score_record(call_record)
        assert result["scores"]["context_recall"] is not None

    def test_composite_is_float(self, mock_evaluate, call_record):
        result = score_record(call_record)
        assert isinstance(result["composite_score"], float)

    def test_original_fields_preserved(self, mock_evaluate, call_record):
        result = score_record(call_record)
        assert result["call_id"] == "test-call-001"
        assert result["model"] == "gemini-flash"
        assert "France" in result["answer"]

    def test_handles_empty_question(self, mock_evaluate, call_record):
        call_record["question"] = ""
        result = score_record(call_record)
        assert "scores" in result
