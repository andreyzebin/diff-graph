from __future__ import annotations

from .base import AgentPRView, AgentPRViewFactory, ProviderError


async def build_proxy(cfg: dict) -> AgentPRView:
    """Build and start an AgentPRView based on cfg['provider']: real (default)."""
    provider_type = cfg.get("provider", "real")
    if provider_type == "real":
        from .real_factory import RealBitbucketFactory
        return await RealBitbucketFactory.build(cfg)
    raise ValueError(f"Unknown proxy provider: {provider_type!r}")
