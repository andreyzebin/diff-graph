from __future__ import annotations

import asyncio
import logging
import os
import shutil
from abc import ABC, abstractmethod

from bitbucket.base import AgentPRView

logger = logging.getLogger(__name__)


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


class CliTrigger(Trigger):
    """
    Runs a local shell command to trigger the agent, then waits for it to exit.

    The *command* string is a shell template that may contain the following
    placeholders (Python .format-style):

      {pr_id}   — integer Bitbucket PR ID
      {pr_url}  — full PR URL built from the Bitbucket connection config

    Because the command is executed via the shell (bash -c …), you can use
    shell features such as ``source .env``, pipes, and variable expansion.

    Example config::

        agent:
          trigger: "cli"
          command: "source .env && python pr_agent/cli.py --pr_url=\\"{pr_url}\\" improve --extended"
          cwd: "/path/to/pr-agent"   # optional; defaults to current directory
          timeout_seconds: 300
    """

    def __init__(
        self,
        command_template: str,
        pr_url_template: str,
        timeout_seconds: int = 300,
        cwd: str | None = None,
        output: str = "log",
    ) -> None:
        self._command_template = command_template
        self._pr_url_template = pr_url_template
        self._timeout = timeout_seconds
        self._cwd = os.path.expanduser(cwd) if cwd else None
        self._output = output  # "log" | "stream"

    async def activate(self, proxy: AgentPRView) -> None:
        pr_url = self._pr_url_template.format(pr_id=proxy.pr_id)
        command = self._command_template.format(pr_id=proxy.pr_id, pr_url=pr_url)

        logger.info("CliTrigger running: %s", command)
        print(f"  → {command}", flush=True)

        proc = await asyncio.create_subprocess_shell(
            command,
            executable="/bin/bash",
            cwd=self._cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stderr_lines: list[str] = []

        async def _stream(stream: asyncio.StreamReader, label: str) -> None:
            async for raw in stream:
                line = raw.decode(errors="replace").rstrip()
                if self._output == "stream":
                    width = shutil.get_terminal_size(fallback=(120, 24)).columns
                    max_len = max(width - 6, 20)
                    truncated = line[:max_len]
                    print(f"\r  ↻  {truncated:<{max_len}}", end="", flush=True)
                else:
                    print(f"  {label} {line}", flush=True)
                if label == "ERR":
                    stderr_lines.append(line)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _stream(proc.stdout, "OUT"),
                    _stream(proc.stderr, "ERR"),
                    proc.wait(),
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(
                f"CliTrigger timed out after {self._timeout}s: {command!r}"
            )
        finally:
            if self._output == "stream":
                print(flush=True)  # завершить текущую строку

        if proc.returncode != 0:
            raise RuntimeError(
                f"CliTrigger exited with code {proc.returncode}:\n"
                + "\n".join(stderr_lines[-20:])
            )
