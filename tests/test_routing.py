"""Verify the complexity router classifies prompts into correct tiers."""
import pytest
from openai import OpenAI


@pytest.fixture(scope="module")
def client(gateway_url, gateway_ready):
    """OpenAI-compatible client pointed at the gateway."""
    return OpenAI(
        base_url=f"{gateway_url}/v1",
        api_key="sk-local-dev-key",
    )


SIMPLE_PROMPTS = [
    ("hello", "greeting"),
    ("what is 2+2?", "basic arithmetic"),
    ("define photosynthesis", "simple definition"),
]

MEDIUM_PROMPTS = [
    ("explain how HTTP works at a high level", "conceptual explanation"),
    ("what are the pros and cons of microservices?", "compare and contrast"),
    ("give me a summary of REST API best practices", "general tech summary"),
]

COMPLEX_PROMPTS = [
    (
        "write a Python async function that queries a database, "
        "processes results with pandas, and returns JSON",
        "code with multiple tech terms",
    ),
    (
        "refactor this class to use dependency injection and add proper error handling",
        "code refactoring",
    ),
    (
        "design a distributed rate limiter architecture using Redis",
        "architecture design",
    ),
]

REASONING_PROMPTS = [
    (
        "step by step, think through how you would debug a memory leak "
        "in a production kubernetes cluster",
        "step-by-step debugging",
    ),
    (
        "analyze this trade-off: consistency vs availability in distributed "
        "databases, and reason through which matters more for a banking system",
        "systems reasoning",
    ),
]


class TestRouting:
    """Black-box routing tests against the gateway."""

    @pytest.mark.parametrize("prompt,category", SIMPLE_PROMPTS)
    def test_simple_prompts_route_to_fast_model(self, client, prompt, category):
        """Simple queries should route to a flash-tier model."""
        resp = client.chat.completions.create(
            model="team-smart-router",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
        )
        model_used = resp.model
        # SIMPLE tier routes to gemini-flash
        assert "flash" in model_used.lower() or "gemini-2.5-flash" in model_used.lower(), (
            f"Expected flash-tier model for '{category}', got {model_used}"
        )

    @pytest.mark.parametrize("prompt,category", COMPLEX_PROMPTS)
    def test_complex_prompts_route_to_pro_model(self, client, prompt, category):
        """Code and architecture prompts should route to a pro-tier model."""
        resp = client.chat.completions.create(
            model="team-smart-router",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
        )
        model_used = resp.model
        # COMPLEX tier routes to gemini-pro
        assert "pro" in model_used.lower() or "gemini-2.5-pro" in model_used.lower(), (
            f"Expected pro-tier model for '{category}', got {model_used}"
        )

    @pytest.mark.parametrize("prompt,category", REASONING_PROMPTS)
    def test_reasoning_prompts_route_to_reasoning_model(self, client, prompt, category):
        """Explicit reasoning requests should route to deepseek-pro."""
        resp = client.chat.completions.create(
            model="team-smart-router",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
        )
        model_used = resp.model
        # REASONING tier routes to deepseek-pro
        assert (
            "pro" in model_used.lower()
            or "deepseek" in model_used.lower()
        ), f"Expected pro/deepseek model for '{category}', got {model_used}"

    def test_router_returns_valid_response(self, client):
        """Router must return a valid chat completion regardless of tier."""
        resp = client.chat.completions.create(
            model="team-smart-router",
            messages=[{"role": "user", "content": "Hello! What can you do?"}],
            max_tokens=50,
        )
        assert resp.choices[0].message.content is not None
        assert len(resp.choices[0].message.content) > 0
