"""Tests for skill injection — registry + middleware.

Covers:
- Unit tests for ``SkillRegistry`` (load, get, list)
- Unit tests for ``SkillInjectorMiddleware`` helpers (trigger detection,
  stripping, system prompt injection)
- Integration test via the live proxy (needs container running)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from proxy.skill_injector import SkillInjectorMiddleware

# ── Skill data for tests ────────────────────────────────────────────────────────

_SAMPLE_SKILL = "# Test Skill\n\nBe minimal. Nothing unnecessary.\n"
_SAMPLE_SKILL_NAME = "testskill"


# ── Fixtures: skill file in a temp skills dir ──────────────────────────────────

@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Create a temporary skills directory with one skill file."""
    d = tmp_path / "skills"
    d.mkdir(parents=True)
    (d / f"{_SAMPLE_SKILL_NAME}.md").write_text(_SAMPLE_SKILL, encoding="utf-8")
    return d


# ── Unit tests: SkillRegistry ──────────────────────────────────────────────────


class TestSkillRegistry:
    """Tests for proxy.skills.registry module."""

    def test_load_and_get(self, skills_dir: Path, monkeypatch) -> None:
        """Skill loaded from .md file is retrievable by stem name."""
        from proxy.skills.registry import load_skills, get, _SKILLS_DIR

        with patch("proxy.skills.registry._SKILLS_DIR", skills_dir):
            load_skills()
        content = get(_SAMPLE_SKILL_NAME)
        assert content is not None
        assert "Be minimal" in content

    def test_get_unknown(self) -> None:
        """Unknown skill name returns None."""
        from proxy.skills.registry import get

        assert get("nonexistent") is None

    def test_list_skills(self, skills_dir: Path, monkeypatch) -> None:
        """list_skills returns all registered skill names."""
        from proxy.skills.registry import load_skills, list_skills, _SKILLS_DIR

        with patch("proxy.skills.registry._SKILLS_DIR", skills_dir):
            load_skills()
        names = list_skills()
        assert _SAMPLE_SKILL_NAME in names

    def test_empty_skipped(self, tmp_path, monkeypatch, caplog) -> None:
        """Empty .md file is skipped with a warning."""
        d = tmp_path / "skills"
        d.mkdir()
        (d / "empty.md").write_text("   ", encoding="utf-8")
        monkeypatch.chdir(d.parent)

        from proxy.skills.registry import load_skills, list_skills

        with patch("proxy.skills.registry._SKILLS_DIR", d):
            load_skills()

        assert list_skills() == []
        assert "empty" in caplog.text.lower() or "empty" in str(caplog.records)


# ── Unit tests: SkillInjectorMiddleware ────────────────────────────────────────


class TestSkillInjector:
    """Direct tests of SkillInjectorMiddleware static helpers."""

    def test_trigger_stripped(self) -> None:
        """Dollar-trigger token is removed from user message content."""
        payload = {
            "messages": [
                {"role": "user", "content": "Refactor this $ponytail please"},
            ],
        }
        with patch("proxy.skill_injector.get_skill") as mock_get:
            mock_get.return_value = _SAMPLE_SKILL
            name, mutated = SkillInjectorMiddleware._detect_and_inject(payload)

        assert name == "ponytail"
        # User message is now at index 1 (system was prepended at 0)
        user_msg = mutated["messages"][1]
        assert user_msg["role"] == "user"
        assert "$ponytail" not in user_msg["content"]
        assert "Refactor this" in user_msg["content"]

    def test_sys_created_when_missing(self) -> None:
        """When no system message exists, one is created from skill content."""
        payload = {
            "messages": [
                {"role": "user", "content": "$ponytail hello"},
            ],
        }
        with patch("proxy.skill_injector.get_skill") as mock_get:
            mock_get.return_value = _SAMPLE_SKILL
            name, mutated = SkillInjectorMiddleware._detect_and_inject(payload)

        assert name == "ponytail"
        messages = mutated["messages"]
        assert messages[0]["role"] == "system"
        assert _SAMPLE_SKILL in messages[0]["content"]
        assert messages[1]["role"] == "user"

    def test_skill_appended(self) -> None:
        """When a system message exists, skill is appended after separator."""
        payload = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "$ponytail Write code"},
            ],
        }
        with patch("proxy.skill_injector.get_skill") as mock_get:
            mock_get.return_value = _SAMPLE_SKILL
            name, mutated = SkillInjectorMiddleware._detect_and_inject(payload)

        assert name == "ponytail"
        system_content = mutated["messages"][0]["content"]
        assert system_content.startswith("You are helpful.")
        assert _SAMPLE_SKILL in system_content

    def test_unknown_trigger(self) -> None:
        """Unrecognised trigger leaves payload unchanged."""
        payload = {
            "messages": [
                {"role": "user", "content": "$" + "unknownskill Write code"},
            ],
        }
        with patch("proxy.skill_injector.get_skill") as mock_get:
            mock_get.return_value = None
            name, mutated = SkillInjectorMiddleware._detect_and_inject(payload)

        assert name is None
        assert mutated == payload

    def test_list_content_handled(self) -> None:
        """Anthropic-style list-of-blocks content IS scanned for triggers."""
        payload = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Hello $ponytail"},
                ]},
            ],
        }
        with patch("proxy.skill_injector.get_skill") as mock_get:
            mock_get.return_value = _SAMPLE_SKILL
            name, mutated = SkillInjectorMiddleware._detect_and_inject(payload)

        assert name == "ponytail"
        # System message was created (list block processing works)
        assert mutated["messages"][0]["role"] == "system"
        # Trigger stripped from the text block
        user_block = mutated["messages"][1]["content"][0]
        assert "$ponytail" not in user_block["text"]
        assert "Hello" in user_block["text"]

    def test_first_only(self) -> None:
        """Only the first recognised trigger is processed."""
        payload = {
            "messages": [
                {"role": "user", "content": "$ponytail do this $other too"},
            ],
        }
        all_skills = {"ponytail": _SAMPLE_SKILL, "other": "# Other skill"}

        def mock_get(name: str) -> str | None:
            return all_skills.get(name)

        with patch("proxy.skill_injector.get_skill", mock_get):
            name, mutated = SkillInjectorMiddleware._detect_and_inject(payload)

        assert name == "ponytail"
        # User message is at index 1 (system was prepended at 0)
        user_msg = mutated["messages"][1]
        assert user_msg["role"] == "user"
        assert "$other" in user_msg["content"]

    def test_header_format(self, skills_dir) -> None:
        """Verify skill name is returned from detection."""
        from proxy.skills.registry import load_skills, get

        with patch("proxy.skills.registry._SKILLS_DIR", skills_dir):
            load_skills()

        skill_content = get(_SAMPLE_SKILL_NAME)
        assert skill_content is not None

        payload = {
            "messages": [
                {"role": "user", "content": "$" + _SAMPLE_SKILL_NAME + " hello"},
            ],
        }
        with patch("proxy.skill_injector.get_skill") as mock_get:
            mock_get.return_value = skill_content
            name, mutated = SkillInjectorMiddleware._detect_and_inject(payload)

        assert name == _SAMPLE_SKILL_NAME

    def test_dupe_guard_true(self) -> None:
        """Idempotency guard: True when skill sig is in system message."""
        skill = "# Skill\n\nBe lazy.\n"
        payload_with = {
            "messages": [
                {"role": "system", "content": "Existing.\n\nBe lazy.\n"},
                {"role": "user", "content": "hello"},
            ],
        }
        assert SkillInjectorMiddleware._already_injected(payload_with, skill) is True

    def test_dupe_guard_false(self) -> None:
        """Idempotency guard: False when skill sig absent."""
        skill = "# Skill\n\nBe lazy.\n"
        payload_without = {
            "messages": [
                {"role": "system", "content": "Different."},
                {"role": "user", "content": "hello"},
            ],
        }
        assert SkillInjectorMiddleware._already_injected(payload_without, skill) is False

    def test_dupe_guard_no_sys(self) -> None:
        """Idempotency guard: False when no system msg exists."""
        skill = "# Skill\n\nBe lazy.\n"
        payload = {
            "messages": [
                {"role": "user", "content": "hello"},
            ],
        }
        assert SkillInjectorMiddleware._already_injected(payload, skill) is False


# ── Integration tests (require running proxy) ─────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("GATEWAY_MASTER_KEY"),
    reason="GATEWAY_MASTER_KEY not set — proxy not running",
)
class TestSkillInjectorIntegration:
    """End-to-end tests against the running GateMid proxy."""

    GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:4000")

    @pytest.fixture
    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {os.environ['GATEWAY_MASTER_KEY']}",
            "Content-Type": "application/json",
        }

    def test_ponytail_header(self, auth_headers) -> None:
        """Sending ponytail should return X-GateMid-Skill-Applied header."""
        payload = {
            "model": "gemini-flash",
            "messages": [
                {"role": "user", "content": "$" + "ponytail Refactor this"},
            ],
        }
        resp = requests.post(
            f"{self.GATEWAY_URL}/chat/completions",
            headers=auth_headers,
            json=payload,
            timeout=30,
        )
        assert resp.status_code != 500, f"Proxy returned 500: {resp.text[:500]}"
        assert resp.headers.get("X-GateMid-Skill-Applied") == "ponytail"

    def test_unknown_no_header(self, auth_headers) -> None:
        """Unknown trigger should not set X-GateMid-Skill-Applied."""
        payload = {
            "model": "gemini-flash",
            "messages": [
                {"role": "user", "content": "$" + "unknownskill Do this"},
            ],
        }
        resp = requests.post(
            f"{self.GATEWAY_URL}/chat/completions",
            headers=auth_headers,
            json=payload,
            timeout=30,
        )
        assert resp.status_code != 500
        assert "X-GateMid-Skill-Applied" not in resp.headers
