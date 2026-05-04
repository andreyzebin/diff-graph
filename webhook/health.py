"""
Background health-check scheduler.

Each entry in `[[health]]` runs a shell command on a fixed interval,
optionally restricted to a daily time window and weekdays. Designed for
keeping rented-GPU vLLM endpoints warm during working hours so the agent
doesn't pay 10+ min cold-start latency on the first real PR comment.

The check command is opaque to the scheduler — usually it's
`python cli.py health --provider <name> -q`, but anything that exits 0
on success is fine.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from .config import HealthCheck

log = logging.getLogger(__name__)


def _parse_window(spec: str) -> Optional[tuple[time, time]]:
    """Parse 'HH:MM-HH:MM' to (start, end). Returns None if empty/invalid."""
    if not spec:
        return None
    try:
        a, b = spec.split("-", 1)
        h1, m1 = a.strip().split(":")
        h2, m2 = b.strip().split(":")
        return time(int(h1), int(m1)), time(int(h2), int(m2))
    except (ValueError, AttributeError):
        log.warning("health: bad time_window %r — ignoring window", spec)
        return None


def _is_active(hc: HealthCheck, now: datetime) -> bool:
    """Should this check fire at `now`?"""
    if hc.days and now.isoweekday() not in hc.days:
        return False
    window = _parse_window(hc.time_window)
    if window is None:
        return True
    start, end = window
    t = now.time()
    if start <= end:
        return start <= t <= end
    # Window crosses midnight (e.g. 22:00-06:00)
    return t >= start or t <= end


async def _run_once(hc: HealthCheck) -> None:
    """Run the check command once, log result. Never raises."""
    if not hc.command:
        return
    started = asyncio.get_event_loop().time()
    try:
        proc = await asyncio.create_subprocess_shell(
            hc.command,
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=hc.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            elapsed = asyncio.get_event_loop().time() - started
            log.warning("health[%s] TIMEOUT after %.1fs (limit %ds)",
                        hc.name, elapsed, hc.timeout_seconds)
            return
    except Exception as exc:
        log.warning("health[%s] launch failed: %s", hc.name, exc)
        return
    elapsed = asyncio.get_event_loop().time() - started
    out = (stdout or b"").decode(errors="replace").strip()
    last_line = out.rsplit("\n", 1)[-1] if out else ""
    if proc.returncode == 0:
        log.info("health[%s] ok %.1fs %s", hc.name, elapsed, last_line[:200])
    else:
        log.warning("health[%s] FAIL exit=%s %.1fs %s",
                    hc.name, proc.returncode, elapsed, last_line[:200])


async def _runner(hc: HealthCheck) -> None:
    """Forever: every interval_seconds, fire if currently in active window."""
    try:
        tz = ZoneInfo(hc.timezone) if hc.timezone else None
    except Exception:
        log.warning("health[%s] bad timezone %r — using UTC", hc.name, hc.timezone)
        tz = None
    log.info("health[%s] scheduled every %ds  window=%s  tz=%s  days=%s",
             hc.name, hc.interval_seconds, hc.time_window or "always",
             hc.timezone, hc.days or "all")
    while True:
        try:
            now = datetime.now(tz) if tz else datetime.now()
            if _is_active(hc, now):
                # Run in background so a slow cold-start (10–15 min) doesn't
                # delay the next interval tick.
                asyncio.create_task(_run_once(hc))
            else:
                log.debug("health[%s] outside window, skipping", hc.name)
            await asyncio.sleep(hc.interval_seconds)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("health[%s] runner error — sleeping and retrying", hc.name)
            await asyncio.sleep(min(hc.interval_seconds, 60))


def start_scheduler(checks: list[HealthCheck]) -> list[asyncio.Task]:
    """Spawn one runner task per check. Returns the tasks for cancellation."""
    return [asyncio.create_task(_runner(hc), name=f"health-{hc.name}")
            for hc in checks if hc.command]
