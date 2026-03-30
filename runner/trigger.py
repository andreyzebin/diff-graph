from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from bitbucket.base import AgentPRView


class Trigger(ABC):
    """Strategy for activating the agent under test on a benchmark PR."""

    @abstractmethod
    async def activate(self, proxy: AgentPRView) -> None:
        """Trigger the agent and wait until it is expected to have finished."""
        ...


class HttpTrigger(Trigger):
    """
    Calls the agent's HTTP endpoint directly.

    Use this when the agent exposes a POST /review endpoint that the
    benchmark can call synchronously.
    """

    def __init__(self, agent_client) -> None:
        self._client = agent_client

    async def activate(self, proxy: AgentPRView) -> None:
        await self._client.run(pr_id=proxy.pr_id)


class WebhookTrigger(Trigger):
    """
    Adds the agent account as a PR reviewer, then waits for *timeout_seconds*.

    Use this when the agent is already integrated via Bitbucket webhooks
    (e.g. PR_REVIEWER_UPDATED event).  Adding the reviewer fires the webhook;
    the benchmark then sleeps long enough for the agent to finish reviewing.
    """

    def __init__(self, agent_account: str, timeout_seconds: int = 120) -> None:
        self._agent_account = agent_account
        self._timeout = timeout_seconds

    async def activate(self, proxy: AgentPRView) -> None:
        await proxy.add_reviewer(self._agent_account)
        await asyncio.sleep(self._timeout)
