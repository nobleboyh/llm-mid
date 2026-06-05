"""GateMid callback shim.

LiteLLM resolves callback paths relative to the config file directory.
This bridges HeadroomCallback 0.23.0 (old callback interface) with
LiteLLM 1.82.3 (new CustomLogger interface).

HeadroomCallback 0.23.0 implements:
  - async_pre_call_hook          (compression — the critical one)
  - async_success_handler        (old name, LiteLLM 1.82.3 doesn't call this)
  - async_failure_handler        (old name, LiteLLM 1.82.3 doesn't call this)

LiteLLM 1.82.3 expects:
  - async_post_call_success_hook   (new name — missing in HeadroomCallback)
  - async_post_call_failure_hook   (new name — missing in HeadroomCallback)
  - async_post_call_streaming_hook (new name — missing in HeadroomCallback)

This shim subclasses HeadroomCallback and adds the missing methods.
Methods are @staticmethod to handle LiteLLM calling them on both
class and instance.

Compression still works because async_pre_call_hook is inherited
from HeadroomCallback and LiteLLM does instantiate for pre-call hooks.
"""

from typing import Any, Optional, Union

import litellm
from headroom.integrations.litellm_callback import HeadroomCallback as _HeadroomCallback


class HeadroomCallback(_HeadroomCallback):
    """Subclass of HeadroomCallback that bridges old→new LiteLLM callback interface."""

    @staticmethod
    async def async_post_call_success_hook(
        user_api_key_dict: Any = None,
        data: dict | None = None,
        response: Union[
            litellm.ModelResponse,
            litellm.EmbeddingResponse,
            litellm.ImageResponse,
            Any,
        ] = None,
    ) -> Any:
        """No-op bridge. Headroom compresses in pre-call, not post-call."""
        return None

    @staticmethod
    async def async_post_call_failure_hook(
        request_data: dict | None = None,
        original_exception: Exception | None = None,
        user_api_key_dict: Any = None,
        traceback_str: Optional[str] = None,
    ) -> Optional[Any]:
        """No-op bridge. Headroom compresses in pre-call, not post-call."""
        return None

    @staticmethod
    async def async_post_call_streaming_hook(
        user_api_key_dict: Any = None,
        response: str | None = None,
    ) -> Any:
        """No-op bridge. Headroom compresses in pre-call, not post-call."""
        return None
