from __future__ import annotations

from .base import BitbucketDataProvider, JiraDataProvider
from .fixture import FixtureBitbucketProvider, FixtureJiraProvider
from .live import LiveBitbucketProvider, LiveJiraProvider
from .overlay import OverlayBitbucketProvider, BitbucketOverrides, OverlayJiraProvider, JiraOverrides


def build_bitbucket_provider(cfg: dict) -> BitbucketDataProvider:
    base_type = cfg.get("base_provider", "fixture")

    if base_type == "fixture":
        base = FixtureBitbucketProvider(cfg.get("data", {}))
    elif base_type == "live":
        base = LiveBitbucketProvider(
            connection=cfg["connection"],
            pull_request_cfg=cfg.get("pull_request"),
        )
    else:
        raise ValueError(f"Unknown provider: {base_type}")

    overrides_data = cfg.get("overrides")
    if overrides_data:
        return OverlayBitbucketProvider(base, BitbucketOverrides(overrides_data))
    return base


def build_jira_provider(cfg: dict) -> JiraDataProvider:
    base_type = cfg.get("base_provider", "fixture")

    if base_type == "fixture":
        base = FixtureJiraProvider(cfg.get("data", {}))
    elif base_type == "live":
        base = LiveJiraProvider(connection=cfg["connection"])
    else:
        raise ValueError(f"Unknown provider: {base_type}")

    overrides_data = cfg.get("overrides")
    if overrides_data:
        return OverlayJiraProvider(base, JiraOverrides(overrides_data))
    return base
