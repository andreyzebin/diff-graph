from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import uvicorn

from fake_servers.bitbucket import create_bitbucket_app
from fake_servers.jira import create_jira_app
from fake_servers.write_sink import InMemoryWriteSink
from fake_servers.providers.base import BitbucketDataProvider, JiraDataProvider, CapturedOutput


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ServersInfo:
    bitbucket_url: str
    jira_url: str
    write_sink: InMemoryWriteSink

    async def get_captured(self) -> CapturedOutput:
        return await self.write_sink.get_captured()

    async def reset(self) -> None:
        await self.write_sink.reset()


class _UvicornServer:
    def __init__(self, app, host: str, port: int):
        self._config = uvicorn.Config(app, host=host, port=port, log_level="error")
        self._server = uvicorn.Server(self._config)
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._server.serve())
        # Wait until startup is complete
        for _ in range(50):
            await asyncio.sleep(0.1)
            if self._server.started:
                break

    async def stop(self):
        self._server.should_exit = True
        if self._task:
            await self._task


@asynccontextmanager
async def FakeServersContext(
    bb_provider: BitbucketDataProvider,
    jira_provider: JiraDataProvider,
    host: str = "127.0.0.1",
) -> AsyncIterator[ServersInfo]:
    write_sink = InMemoryWriteSink()

    bb_port = _find_free_port()
    jira_port = _find_free_port()

    bb_app = create_bitbucket_app(bb_provider, write_sink)
    jira_app = create_jira_app(jira_provider)

    bb_server = _UvicornServer(bb_app, host, bb_port)
    jira_server = _UvicornServer(jira_app, host, jira_port)

    await bb_server.start()
    await jira_server.start()

    try:
        yield ServersInfo(
            bitbucket_url=f"http://{host}:{bb_port}",
            jira_url=f"http://{host}:{jira_port}",
            write_sink=write_sink,
        )
    finally:
        await bb_server.stop()
        await jira_server.stop()
