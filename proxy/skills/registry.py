"""Skill registry — loads markdown skill files from the skills directory at startup.

Each ``.md`` file in ``proxy/skills/`` is keyed by its stem (lowercased filename
without extension) and made available via ``get(name)``. A skill is a short
markdown document that constrains LLM behaviour when activated by a ``$trigger``
token in the user message.

Usage::

    from proxy.skills.registry import load_skills, get, list_skills

    load_skills()          # call once at startup
    content = get("ponytail")   # returns str | None
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent
_registry: dict[str, str] = {}


def load_skills() -> None:
    """Scan ``_SKILLS_DIR`` for ``.md`` files and populate the registry.

    Safe to call multiple times — re-reading replaces the previous snapshot.
    Empty files are logged at WARNING and skipped.
    """
    _registry.clear()
    for skill_file in sorted(_SKILLS_DIR.glob("*.md")):
        name = skill_file.stem.lower()
        content = skill_file.read_text(encoding="utf-8").strip()
        if content:
            _registry[name] = content
            logger.info(
                "[SkillRegistry] Loaded skill '%s' (%d chars)",
                name,
                len(content),
            )
        else:
            logger.warning(
                "[SkillRegistry] Skill file '%s' is empty — skipped",
                skill_file.name,
            )

    loaded = len(_registry)
    if loaded:
        logger.info(
            "[SkillRegistry] Loaded %d skill(s): %s",
            loaded,
            ", ".join(sorted(_registry)),
        )
    else:
        logger.info("[SkillRegistry] No skill files found in %s", _SKILLS_DIR)


def get(skill_name: str) -> str | None:
    """Return the skill content for *skill_name*, or ``None`` if unknown."""
    return _registry.get(skill_name.lower())


def list_skills() -> list[str]:
    """Return a sorted list of all registered skill names."""
    return sorted(_registry)
