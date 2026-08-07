"""Tests for LocalProviderFailureLogger — LiteLLM failure diagnostics callback.

Covers both sync and async failure hooks, model-prefix and api_base-port
detection, the 404-only gate, and the port-matching guard against false
positives (`:8080` must not match inside `:18080`).
"""

import asyncio

import pytest

from proxy.callbacks.local_provider_failure import LocalProviderFailureLogger

LOGGER_NAME = "proxy.callbacks.local_provider"


def run(fn):
    """Run an async callback hook to completion."""
    asyncio.run(fn)


@pytest.fixture
def logger():
    return LocalProviderFailureLogger()


def _404_kwargs(model, api_base, exc="NotFoundError: 404 model not found"):
    return {
        "model": model,
        "exception": exc,
        "litellm_params": {"api_base": api_base},
    }


# ── Detection: model-name prefix ─────────────────────────────────────────

def test_ollama_prefix_detected(caplog, logger):
    with caplog.at_level("ERROR", logger=LOGGER_NAME):
        run(logger.async_log_failure_event(
            kwargs=_404_kwargs("ollama/llama3", "http://localhost:11434"),
            response_obj=None, start_time=None, end_time=None,
        ))
    assert "Ollama" in caplog.text
    assert "OLLAMA_MODEL" in caplog.text


def test_lmstudio_prefix_detected(caplog, logger):
    with caplog.at_level("ERROR", logger=LOGGER_NAME):
        run(logger.async_log_failure_event(
            kwargs=_404_kwargs("lm_studio/model", "http://localhost:1234"),
            response_obj=None, start_time=None, end_time=None,
        ))
    assert "LM Studio" in caplog.text
    assert "LMSTUDIO_MODEL" in caplog.text


# ── Detection: api_base port heuristic (openai/-prefixed local providers) ─

def test_openai_prefixed_llamacpp_via_port(caplog, logger):
    # llama.cpp/oMLX use the generic "openai/" prefix — identified by :8080
    with caplog.at_level("ERROR", logger=LOGGER_NAME):
        run(logger.async_log_failure_event(
            kwargs=_404_kwargs("llama-3.2-3b", "http://localhost:8080/v1"),
            response_obj=None, start_time=None, end_time=None,
        ))
    assert "llama.cpp" in caplog.text
    assert "LLAMACPP_MODEL" in caplog.text


def test_openai_prefixed_omlx_via_port(caplog, logger):
    with caplog.at_level("ERROR", logger=LOGGER_NAME):
        run(logger.async_log_failure_event(
            kwargs=_404_kwargs("llama", "http://localhost:8000/v1"),
            response_obj=None, start_time=None, end_time=None,
        ))
    assert "oMLX" in caplog.text
    assert "OMLX_MODEL" in caplog.text


# ── Gates: only 404 / not-found failures, and only local providers ───────

def test_non_404_error_is_skipped(caplog, logger):
    with caplog.at_level("ERROR", logger=LOGGER_NAME):
        run(logger.async_log_failure_event(
            kwargs=_404_kwargs(
                "ollama/llama3", "http://localhost:11434",
                exc="AuthenticationError: 401 invalid key",
            ),
            response_obj=None, start_time=None, end_time=None,
        ))
    assert caplog.text == ""


def test_cloud_provider_is_skipped(caplog, logger):
    with caplog.at_level("ERROR", logger=LOGGER_NAME):
        run(logger.async_log_failure_event(
            kwargs=_404_kwargs(
                "gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1",
            ),
            response_obj=None, start_time=None, end_time=None,
        ))
    assert caplog.text == ""


# ── Both hooks share the same behavior ───────────────────────────────────

def test_sync_hook_fires_too(caplog, logger):
    with caplog.at_level("ERROR", logger=LOGGER_NAME):
        logger.log_failure_event(
            kwargs=_404_kwargs("ollama/llama3", "http://localhost:11434"),
            response_obj=None, start_time=None, end_time=None,
        )
    assert "Ollama" in caplog.text


# ── Port guard: no substring false positives ─────────────────────────────

@pytest.mark.parametrize("api_base,expected", [
    ("http://localhost:11434", "Ollama"),
    ("http://localhost:8080", "llama.cpp"),
    ("http://localhost:1234", "LM Studio"),
    ("http://localhost:8000", "oMLX"),
    ("http://localhost:18080", None),   # :8080 inside :18080 must not match
    ("http://localhost:114341", None),  # :11434 inside a longer number
    ("http://localhost:9999", None),
])
def test_port_heuristic(api_base, expected, logger):
    got = logger._api_base_is_local(api_base)
    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert got["label"] == expected
