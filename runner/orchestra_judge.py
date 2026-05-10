"""OrchestraJudge — Phase 2.C of the OTel migration.

The bench's existing LLMJudge gathers context (PR comments, diff,
intended_findings, AGENTS.md, …) and renders a single prompt that
asks an LLM to produce a JSON verdict. This module replaces only
the final LLM call: instead of calling the LLM in-process, we shell
out to diff-graph's CLI and run the `judge.raw` orchestra agent.

Why: judge runs then go through the SAME instrumentation stack as
the agents they grade — OTel spans, payload files, /qa/runs filter,
/qa/scoring, distributed-trace propagation via TRACEPARENT. No
special "judge tracing" code path anywhere.

How to use:

    judge_cfg["backend"] = "orchestra"  # in config.local.yaml
    judge_cfg["diffgraph_repo"] = "/home/andrey/repos/diff-graph"
    judge_cfg["agent"] = "judge.raw"

The bench's `_make_llm_client` then returns `_SubprocessLLMClient`
instead of OpenAILLMClient/AnthropicLLMClient; the rest of LLMJudge
is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


class SubprocessLLMClient:
    """LLMClient stand-in: shells out to `diffgraph cli.py run --agent=judge.raw`.

    Stays drop-in replaceable for the bench's existing
    `OpenAILLMClient.complete_json(prompt) -> dict` interface, which is
    the only LLMJudge entry point. Trace-context propagation works
    through the worker → bench → cli.py chain because we just inherit
    the parent process's TRACEPARENT env var.
    """

    def __init__(self,
                 diffgraph_repo: str | Path = "/home/andrey/repos/diff-graph",
                 agent: str = "judge.raw",
                 prompts_subdir: str = "diffgraph/prompts/judges/",
                 timeout: int = 600,
                 model: str = "",
                 stream_output: bool = False):
        self._repo = Path(diffgraph_repo).expanduser().resolve()
        self._agent = agent
        self._prompts_dir = (self._repo / prompts_subdir).resolve()
        self._timeout = timeout
        # Optional: forward provider/model via cli.py flags so the bench
        # config still controls which LLM does the judging.
        self._model = model
        self._stream_output = stream_output

    def complete_json(self, prompt: str) -> dict:
        """Run cli.py run, return the parsed JSON the judge produced."""
        out_fd, out_path = tempfile.mkstemp(suffix=".json", prefix="orcjudge-")
        os.close(out_fd)
        try:
            cmd = [
                str(self._repo / ".venv" / "bin" / "python"),
                str(self._repo / "cli.py"), "run",
                f"--agent={self._agent}",
                f"--prompts={self._prompts_dir}",
                "--output", out_path,
                "--user-message", prompt,
            ]
            if self._model:
                cmd.extend(["--model", self._model])
            log.info("OrchestraJudge: shelling out to %s", " ".join(cmd[:6]))
            r = subprocess.run(cmd, cwd=str(self._repo),
                                capture_output=True, text=True,
                                timeout=self._timeout)
            if r.returncode != 0:
                raise RuntimeError(
                    f"orchestra judge subprocess failed (code "
                    f"{r.returncode}): {r.stderr[-500:] or r.stdout[-500:]}"
                )
            data_text = Path(out_path).read_text(encoding="utf-8")
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"orchestra judge produced invalid JSON: {exc}\n"
                    f"raw[:500]: {data_text[:500]!r}"
                )
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"orchestra judge returned non-dict: {type(data).__name__}"
                )
            return data
        finally:
            try:
                Path(out_path).unlink()
            except OSError:
                pass

    def complete_text(self, prompt: str) -> str:
        """Same as complete_json but returns the response as text. Not
        used by the current LLMJudge but kept for symmetry with the
        legacy LLMClient interface."""
        d = self.complete_json(prompt)
        return json.dumps(d, ensure_ascii=False)
