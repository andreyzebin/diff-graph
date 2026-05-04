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
        interaction_command_template: str | None = None,
    ) -> None:
        self._command_template = command_template
        # Used when a scenario fires through a comment (/help, /ask, …)
        # — drives the dispatcher path of cli.py with --message and
        # --comment-id. Optional: scenarios that don't use comment
        # triggers won't touch this.
        self._interaction_command_template = interaction_command_template
        self._pr_url_template = pr_url_template
        self._timeout = timeout_seconds
        self._cwd = os.path.expanduser(cwd) if cwd else None
        self._output = output  # "log" | "stream"

    async def activate(self, proxy: AgentPRView, **extra_placeholders) -> None:
        """
        Run the configured shell command. Caller can pass extra placeholder
        values to substitute (e.g. {message}, {comment_id} for interaction
        scenarios that drive the dispatcher path of cli.py).

        When `message` is provided in extra_placeholders AND
        interaction_command_template is configured, that template is
        used instead of the default command. This lets a single benchmark
        config support both review (--agent reviewer) and interaction
        (--message ... --comment-id ...) flows.
        """
        is_interaction = bool(extra_placeholders.get("message"))
        if is_interaction and self._interaction_command_template:
            template = self._interaction_command_template
        else:
            template = self._command_template

        pr_url = self._pr_url_template.format(pr_id=proxy.pr_id)
        # Defaults: keep placeholders that the template might reference
        # but the caller didn't fill — substituted with empty strings so
        # `--message="{message}"` becomes `--message=""`.
        ctx = {"message": "", "comment_id": "", "provider": ""}
        ctx.update(extra_placeholders)
        ctx["pr_id"] = proxy.pr_id
        ctx["pr_url"] = pr_url
        try:
            command = template.format(**ctx)
        except KeyError as exc:
            raise RuntimeError(
                f"CliTrigger: command template references unknown "
                f"placeholder {exc} (known: {sorted(ctx)})"
            ) from exc

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
