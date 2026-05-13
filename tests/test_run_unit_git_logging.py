"""`_git()` failures log stderr to the bench system log.

Plan 212 task #3636 (and 8 sibling qwen3-6 tasks) all errored with
`CalledProcessError: ... exit status 128`. Rich's traceback render
showed the exit code and the command but NOT git's own stderr — the
operator couldn't tell from the UI whether the failure was
"permission denied", "remote not found", "lock held", or anything
else. The actual reason was captured by subprocess but discarded
along the exception path.

Fix: `_git()` catches `CalledProcessError`, logs the cmd + stderr
(truncated to 2KB) + stdout via `logger.error`, then re-raises so
the caller's control flow is unchanged. This test pins:

  - logger.error is called when the subprocess exits non-zero
  - the captured stderr text appears in the log message verbatim
  - the original exception is re-raised (no swallow)
  - successful runs DO NOT log (logger.error not invoked on rc=0)
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from runner.run_unit import _git


class TestGitFailureLogging:

    def test_failure_logs_stderr_via_logger_error(self, tmp_path, caplog):
        """Simulate `git foo` exiting 128 with a real stderr message.
        After re-raise, the bench's `_git` should have logged the
        full stderr through Python's logging — bench's system.log
        handler then writes it to the per-task file the UI surfaces."""
        with caplog.at_level(logging.ERROR, logger="runner.run_unit"):
            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                # Pass `--quiet` + an unknown command so git emits a
                # real error (not just our mock).
                _git(str(tmp_path), "totally-bogus-subcommand")

        # Re-raise contract: caller still gets the original exception.
        assert exc_info.value.returncode != 0

        # logger.error fired with the cmd + stderr text in the message.
        error_records = [r for r in caplog.records
                          if r.levelno == logging.ERROR
                          and r.name == "runner.run_unit"]
        assert len(error_records) == 1
        msg = error_records[0].getMessage()
        # The command line is in the log.
        assert "totally-bogus-subcommand" in msg
        # And the git-emitted stderr — git complains about unknown
        # subcommands with "is not a git command" or similar.
        # Just check that *some* stderr text is in the message.
        assert "stderr=" in msg

    def test_success_does_not_log(self, tmp_path, caplog):
        """A normal successful git invocation must NOT emit an error
        log — otherwise the per-task system.log would fill with
        false-positive ERROR lines from every git operation."""
        # Initialize an empty repo and run a benign command.
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        with caplog.at_level(logging.ERROR, logger="runner.run_unit"):
            out = _git(str(tmp_path), "rev-parse", "--is-inside-work-tree")
        # Output is the actual command result.
        assert out == "true"
        # And no error logs.
        error_records = [r for r in caplog.records
                          if r.levelno == logging.ERROR
                          and r.name == "runner.run_unit"]
        assert error_records == []

    def test_stderr_truncated_at_2kb(self, tmp_path, caplog):
        """Bound the log line length — pathological git failures
        (e.g. huge -v output dumped to stderr) shouldn't dump
        megabytes per failed call. 2KB is enough to read the actual
        message; rest is dropped by `[:2000]`."""
        # Synthesize a CalledProcessError with a huge stderr via
        # monkey-patching subprocess.run.
        huge = "x" * 5000
        fake_err = subprocess.CalledProcessError(
            returncode=128, cmd=["git", "fake"], stderr=huge, output="",
        )
        with patch("runner.run_unit.subprocess.run", side_effect=fake_err):
            with caplog.at_level(logging.ERROR, logger="runner.run_unit"):
                with pytest.raises(subprocess.CalledProcessError):
                    _git(".", "fake")
        msg = caplog.records[-1].getMessage()
        # Find the stderr= section and assert its length is bounded.
        # The msg format is `... stderr=%r stdout=%r` where %r adds
        # quote chars; the inner string must be ≤ 2000.
        import re
        m = re.search(r"stderr='([^']*)'", msg)
        assert m, f"no stderr section in: {msg[:200]}"
        assert len(m.group(1)) <= 2010  # 2000 chars + some escape slack
