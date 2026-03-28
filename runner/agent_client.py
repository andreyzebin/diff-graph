from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass
class AgentResponse:
    success: bool
    raw: dict


class AgentClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: int = 120):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get("AGENT_API_KEY", "")
        self._timeout = timeout

    async def run(
        self,
        pr_id: int,
        project: str = "BENCH",
        repo: str = "test-repo",
    ) -> AgentResponse:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "pr_id": pr_id,
            "project": project,
            "repo": repo,
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/review",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return AgentResponse(success=True, raw=resp.json())
