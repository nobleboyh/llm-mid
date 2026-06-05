"""GateMid callback shims.

LiteLLM resolves callback paths relative to the config file directory.
This shim imports the actual HeadroomCallback from the installed package.
"""
from headroom.integrations.litellm_callback import HeadroomCallback  # noqa: F401
