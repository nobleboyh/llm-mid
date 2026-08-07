"""Guard tests: no deployment may trigger live LLM probes from /health.

Enforces the init-007 fix — every deployment must carry
model_info.disable_background_health_check: true AND general_settings must
have background_health_checks: true. Without either, /health or the
background loop issues real billed completions.
"""

import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIGS = ["litellm_config.example.yaml", "litellm_config.test.yaml"]


@pytest.mark.parametrize("config_name", CONFIGS)
def test_background_health_checks_enabled(config_name):
    config = yaml.safe_load((ROOT / config_name).read_text())
    gs = config.get("general_settings", {})
    assert gs.get("background_health_checks") is True, (
        f"{config_name}: general_settings.background_health_checks must be true"
    )


@pytest.mark.parametrize("config_name", CONFIGS)
def test_no_deployment_live_probes(config_name):
    config = yaml.safe_load((ROOT / config_name).read_text())
    deployments = config.get("model_list", [])
    assert deployments, f"{config_name}: model_list must be non-empty"
    for dep in deployments:
        name = dep.get("model_name", "<unnamed>")
        mi = dep.get("model_info") or {}
        assert mi.get("disable_background_health_check") is True, (
            f"{config_name}: deployment '{name}' must set "
            "model_info.disable_background_health_check: true"
        )
