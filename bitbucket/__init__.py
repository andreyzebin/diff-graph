from __future__ import annotations

from .base import BitbucketPRProxy, BitbucketFactory, ProviderError


async def build_proxy(cfg: dict) -> BitbucketPRProxy:
    """Build and start a proxy based on cfg['provider']: real (default)."""
    provider_type = cfg.get("provider", "real")
    if provider_type == "real":
        from .real_factory import RealBitbucketFactory
        return await RealBitbucketFactory.build(cfg)
    raise ValueError(f"Unknown proxy provider: {provider_type!r}")
