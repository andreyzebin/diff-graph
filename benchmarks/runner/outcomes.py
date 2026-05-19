"""outcomes.yaml — ground-truth labels + miss/noise/stability metrics.

See TODO §19.9. This module:

  1. Defines the `outcomes.yaml` schema (labels per agent comment +
     missed_findings list).
  2. Auto-infers labels from human reactions captured in the
     recording's timeline (resolve-without-counter-reply → valid,
     ❌/[noise]/explicit-negation reply → noise).
  3. Scores a `LifecycleReplayResult` against the labels and emits
     business metrics: miss_rate, miss_rate_blocker,
     cumulative_noise_rate, lifecycle_stability,
     convergence_invocations, drift_alerts.

The file is OPTIONAL — a recording without `outcomes.yaml` still
produces a useful LifecycleReplayResult from per-invocation judge
scores alone. Outcomes graduates the result to business metrics.

Schema (matches TODO §19.9):

    labels:
      a-001-01:
        verdict: valid | noise | undecided
        confidence: high | medium | low
        source: auto | manual
        rationale: "<why>"
    missed_findings:
      - human_comment_stable_id: h-012
        severity: BLOCKER | MAJOR | MINOR | COMMENT
        topic: "<short description>"
    labelled_by: alice@example.com
    labelled_at: 2026-06-01T...
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Schema dataclasses ───────────────────────────────────────────────────


@dataclass
class CommentLabel:
    verdict: str           # "valid" | "noise" | "undecided"
    confidence: str        # "high" | "medium" | "low"
    source: str            # "auto" | "manual"
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "verdict":    self.verdict,
            "confidence": self.confidence,
            "source":     self.source,
            "rationale":  self.rationale,
        }


@dataclass
class MissedFinding:
    human_comment_stable_id: str
    severity: str          # "BLOCKER" | "MAJOR" | "MINOR" | "COMMENT"
    topic: str = ""

    def to_dict(self) -> dict:
        return {
            "human_comment_stable_id": self.human_comment_stable_id,
            "severity":                self.severity,
            "topic":                   self.topic,
        }


@dataclass
class Outcomes:
    labels: dict[str, CommentLabel] = field(default_factory=dict)
    missed_findings: list[MissedFinding] = field(default_factory=list)
    labelled_by: str = ""
    labelled_at: str = ""

    def to_dict(self) -> dict:
        return {
            "labels":          {k: v.to_dict() for k, v in self.labels.items()},
            "missed_findings": [m.to_dict() for m in self.missed_findings],
            "labelled_by":     self.labelled_by,
            "labelled_at":     self.labelled_at,
        }


# ── Load / save ──────────────────────────────────────────────────────────


def load_outcomes(path: Path | str) -> Optional[Outcomes]:
    """Load outcomes.yaml. Returns None on missing file. Raises
    ValueError on malformed shape — broken labels are a hard error,
    not silently ignored (the metrics layer leans on this contract)."""
    import yaml
    p = Path(path)
    if not p.is_file():
        return None
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: outcomes.yaml must be a mapping, got {type(raw).__name__}")
    out = Outcomes(
        labelled_by=str(raw.get("labelled_by", "")),
        labelled_at=str(raw.get("labelled_at", "")),
    )
    for sid, lbl in (raw.get("labels") or {}).items():
        if not isinstance(lbl, dict):
            continue
        verdict = str(lbl.get("verdict", "undecided")).lower()
        if verdict not in ("valid", "noise", "undecided"):
            raise ValueError(
                f"{p}: label {sid!r} has invalid verdict={verdict!r}"
            )
        out.labels[str(sid)] = CommentLabel(
            verdict=verdict,
            confidence=str(lbl.get("confidence", "medium")).lower(),
            source=str(lbl.get("source", "manual")).lower(),
            rationale=str(lbl.get("rationale", "")),
        )
    for entry in (raw.get("missed_findings") or []):
        if not isinstance(entry, dict):
            continue
        out.missed_findings.append(MissedFinding(
            human_comment_stable_id=str(entry.get("human_comment_stable_id", "")),
            severity=str(entry.get("severity", "MINOR")).upper(),
            topic=str(entry.get("topic", "")),
        ))
    return out


def save_outcomes(out: Outcomes, path: Path | str) -> None:
    import yaml
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(out.to_dict(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── Auto-inference from recording ────────────────────────────────────────


# Patterns lifted from the rationale in TODO §19.9. Tuned to be
# tolerant (lowercase, allow surrounding text) but precise enough not
# to mark every "thanks" as agreement.
_NOISE_MARKERS = [
    re.compile(r"^\s*❌"),
    re.compile(r"\[noise\]", re.IGNORECASE),
    re.compile(r"\bне\s+согласен\b", re.IGNORECASE),
    re.compile(r"\bне\s+верно\b", re.IGNORECASE),
    re.compile(r"\bwrong\b", re.IGNORECASE),
    re.compile(r"\bdisagree\b", re.IGNORECASE),
    re.compile(r"\bне\s+критично\b", re.IGNORECASE),
    re.compile(r"\bне\s+нужно\b", re.IGNORECASE),
    re.compile(r"\bfalse[\-\s]?positive\b", re.IGNORECASE),
]

_VALID_MARKERS = [
    re.compile(r"^\s*\+1\b"),
    re.compile(r"\bvalid\b", re.IGNORECASE),
    re.compile(r"\bnice catch\b", re.IGNORECASE),
    re.compile(r"\bgood catch\b", re.IGNORECASE),
    re.compile(r"\bхорошо подмечено\b", re.IGNORECASE),
    # "согласен" but NOT "не согласен" — negative lookbehind on the
    # negation particle. Same for the English fix/fixed markers
    # ("not fixed" / "won't fix" stay out of valid).
    re.compile(r"(?<!не\s)\bсогласен\b", re.IGNORECASE),
    re.compile(r"(?<!not\s)(?<!won't\s)\bfix(?:ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bпоправлю\b", re.IGNORECASE),
]


def _classify_reply(body: str) -> Optional[str]:
    """Crude marker-based classifier. Returns 'valid', 'noise', or
    None (no signal). Conservative: a body matching BOTH a noise
    and a valid marker → None (ambiguous). The LLM-classifier
    fallback (TODO §19.9) is a follow-up; this gets us off the
    ground without one."""
    is_noise = any(p.search(body) for p in _NOISE_MARKERS)
    is_valid = any(p.search(body) for p in _VALID_MARKERS)
    if is_noise and is_valid:
        return None
    if is_noise:
        return "noise"
    if is_valid:
        return "valid"
    return None


def auto_infer(rec_loader_func, rec_pr_dir: Path) -> Outcomes:
    """Walk the recording's timeline and infer labels.

    Rules (TODO §19.9):
      - Agent comment + human reply matching noise marker → noise (high).
      - Agent comment + human reply matching valid marker → valid (high).
      - Agent comment + thread resolved without counter-reply → valid (medium).
      - Disagreement-via-LLM-classifier: not implemented here; left
        for a follow-up.
      - Anything else → undecided.

    Args:
        rec_loader_func: callable that takes rec_pr_dir and returns
            a Recording (passed in so this module doesn't import
            replay.py — keeps the import graph clean).
        rec_pr_dir: PR directory of the recording.

    Returns: Outcomes with `labels` populated. `missed_findings` is
    NOT auto-inferred — that requires topic clustering / incident
    links and remains a manual nominate-then-confirm step.
    """
    rec = rec_loader_func(rec_pr_dir)
    out = Outcomes(labelled_at="auto")

    # Build a flat view: every agent comment ever captured + every
    # human comment, in timeline order. Agent comments come from
    # invocations[*].output.posted_comments (stamped with a-NNN-K).
    # Human comments come from invocations[*].snapshot.comments
    # where author != bot (we approximate via is_bot field).
    agent_comments: dict[str, dict] = {}      # stable_id → {body, bb_id, inv_idx}
    human_comments: list[dict] = []           # ordered
    human_by_stable: dict[str, dict] = {}
    bb_to_stable_agent: dict[int, str] = {}

    for inv in rec.invocations:
        out_data = inv.output or {}
        for c in (out_data.get("posted_comments") or []):
            sid = c.get("stable_id")
            if not sid:
                continue
            agent_comments[sid] = {
                "body":    c.get("body", "") or c.get("text", ""),
                "bb_id":   c.get("bb_id"),
                "inv_idx": inv.index,
            }
            if isinstance(c.get("bb_id"), int):
                bb_to_stable_agent[c["bb_id"]] = sid

        # Comments in this invocation's snapshot — gather humans only.
        for c in (inv.snapshot.get("comments") or []):
            if c.get("is_bot"):
                # Skip bot comments — those are the recorded agent's,
                # we already see them through posted_comments above.
                continue
            sid = c.get("stable_id") or ""
            if sid in human_by_stable:
                # Track resolved-state change across snapshots.
                if c.get("resolved") and not human_by_stable[sid].get("resolved"):
                    human_by_stable[sid]["resolved"] = True
                continue
            entry = {
                "stable_id":         sid,
                "bb_id":             c.get("bb_id"),
                "body":              c.get("body", "") or c.get("text", ""),
                "parent_stable_id":  c.get("parent_stable_id"),
                "resolved":          bool(c.get("resolved", False)),
                "first_seen_inv":    inv.index,
            }
            human_comments.append(entry)
            human_by_stable[sid] = entry

    # For each human comment that REPLIES to an agent comment, derive
    # the label of the agent comment it replies to.
    inferred_by_agent: dict[str, list[CommentLabel]] = {}
    for hc in human_comments:
        parent = hc.get("parent_stable_id") or ""
        if parent not in agent_comments:
            continue
        cls = _classify_reply(hc.get("body", ""))
        if cls is None:
            continue
        inferred_by_agent.setdefault(parent, []).append(CommentLabel(
            verdict=cls, confidence="high", source="auto",
            rationale=f"human reply ({hc.get('stable_id')}) matched "
                       f"{cls}-marker",
        ))

    # Resolved-without-counter-reply case: walk threads, find agent
    # comments whose thread became `resolved` and which got NO reply.
    # Approximation: a thread is "resolved" if the original agent
    # comment was marked resolved=True in some later snapshot — but
    # snapshot comments are HUMAN comments only here. So we look at
    # the FULL captured comment graph (including bot ones we filtered
    # out) for resolution status of agent comments.
    for inv in rec.invocations:
        for c in (inv.snapshot.get("comments") or []):
            if not c.get("is_bot"):
                continue
            bb_id = c.get("bb_id")
            if not isinstance(bb_id, int):
                continue
            sid_agent = bb_to_stable_agent.get(bb_id)
            if not sid_agent:
                continue
            # If thread became resolved AND no replies inferred either way:
            if c.get("resolved") and sid_agent not in inferred_by_agent:
                inferred_by_agent[sid_agent] = [CommentLabel(
                    verdict="valid", confidence="medium", source="auto",
                    rationale="thread resolved with no counter-reply",
                )]

    # Conflict resolution per agent comment: if multiple signals point
    # the same way → keep highest confidence. If they conflict →
    # undecided (downgrade rather than guess).
    for sid_agent, signals in inferred_by_agent.items():
        verdicts = {s.verdict for s in signals}
        if len(verdicts) > 1:
            out.labels[sid_agent] = CommentLabel(
                verdict="undecided", confidence="low", source="auto",
                rationale=f"conflicting signals: {sorted(verdicts)}",
            )
        else:
            best = max(signals,
                       key=lambda s: {"low": 0, "medium": 1, "high": 2}.get(
                           s.confidence, 0))
            out.labels[sid_agent] = best

    return out


# ── Metrics ──────────────────────────────────────────────────────────────


@dataclass
class LifecycleMetrics:
    """Business metrics for one lifecycle replay run."""
    # Per-finding counts ───────────────────────────────────
    n_findings_total: int = 0          # what the agent posted (replay)
    n_findings_valid: int = 0          # matched to a `valid` label
    n_findings_noise: int = 0          # matched to a `noise` label
    n_findings_unlabeled: int = 0      # matched to a `undecided` label
                                       # OR no recorded label (no human reaction)
    # Misses ───────────────────────────────────────────────
    n_missed_total: int = 0            # missed_findings size
    n_missed_blocker: int = 0          # missed_findings filtered to BLOCKER/MAJOR
    # Aggregate rates ──────────────────────────────────────
    miss_rate: float = 0.0
    miss_rate_blocker: float = 0.0
    cumulative_noise_rate: float = 0.0
    # Per-invocation breakdown ─────────────────────────────
    convergence_invocations: dict[str, int] = field(default_factory=dict)
    drift_alerts: list[dict] = field(default_factory=list)
    # Replay stability ─────────────────────────────────────
    orphan_skip_count: int = 0

    def to_dict(self) -> dict:
        return {
            "n_findings_total":      self.n_findings_total,
            "n_findings_valid":      self.n_findings_valid,
            "n_findings_noise":      self.n_findings_noise,
            "n_findings_unlabeled":  self.n_findings_unlabeled,
            "n_missed_total":        self.n_missed_total,
            "n_missed_blocker":      self.n_missed_blocker,
            "miss_rate":             round(self.miss_rate, 4),
            "miss_rate_blocker":     round(self.miss_rate_blocker, 4),
            "cumulative_noise_rate": round(self.cumulative_noise_rate, 4),
            "convergence_invocations": self.convergence_invocations,
            "drift_alerts":          self.drift_alerts,
            "orphan_skip_count":     self.orphan_skip_count,
        }


def score_lifecycle(
    *, replay_result: Any,
    outcomes: Outcomes,
    recorded_findings_by_invocation: dict[int, list[dict]],
) -> LifecycleMetrics:
    """Aggregate metrics from a LifecycleReplayResult + outcomes.yaml.

    Args:
        replay_result: a LifecycleReplayResult (we duck-type it to
            avoid the circular import).
        outcomes: parsed outcomes.yaml.
        recorded_findings_by_invocation: map invocation_index →
            list of finding dicts, AS RECORDED ORIGINALLY. Used to
            compute convergence_invocations / drift_alerts (we
            still need to know which recorded finding maps to which
            label — outcomes.labels keys agent comments by stable_id
            which is `a-NNN-K`, so an exact lookup).

    Algorithm:
      For each invocation in replay_result.invocations:
        For each posted_comment c by the replay agent:
          - Try to label by topic-matching against agent comments in
            the recording. We approximate by treating ANY agent
            comment from the recording with the same `file:line` and
            similar topic as a match. Without LLM-based topic match
            we fall back to "uncategorized".
        Aggregate counts → rates.
      Misses come straight from outcomes.missed_findings; rate
      denominator = total human-validated findings (missed + valid
      we matched). Blocker rate filters to BLOCKER+MAJOR severity.
      convergence_invocations[finding_topic] = smallest invocation N
      where the replay agent first posted a comment matching the
      finding's recorded a-NNN-K label.
      drift_alerts[] entries are filed when a verified finding
      appears in invocation N and disappears in N+M (M >= 1).

    Topic matching across replay-vs-recorded is intentionally
    minimal here — Phase 3 ships the metrics scaffold; richer
    matching is the LLM-classifier follow-up.
    """
    m = LifecycleMetrics()
    m.n_missed_total = len(outcomes.missed_findings)
    m.n_missed_blocker = sum(
        1 for mf in outcomes.missed_findings
        if mf.severity in ("BLOCKER", "MAJOR")
    )
    m.orphan_skip_count = getattr(replay_result, "orphan_skip_count", 0) or 0

    # Walk each invocation's REPLAY-posted comments.
    seen_topics_per_inv: dict[int, set[str]] = {}
    for inv in (replay_result.invocations or []):
        idx = inv.index
        seen_topics_per_inv.setdefault(idx, set())
        for posted in (inv.posted_comments or []):
            m.n_findings_total += 1
            # Match by file:line against the recorded baseline.
            file_ = posted.get("file") or ""
            line = posted.get("line") or 0
            topic_key = f"{file_}:{line}"
            seen_topics_per_inv[idx].add(topic_key)

            # Look up the recorded label for the closest-matching
            # agent comment.
            label = _match_label_for_replay_comment(
                file_, line, idx,
                recorded_findings_by_invocation, outcomes,
            )
            if label is None:
                m.n_findings_unlabeled += 1
                continue
            if label.verdict == "valid":
                m.n_findings_valid += 1
                m.convergence_invocations.setdefault(topic_key, idx)
            elif label.verdict == "noise":
                m.n_findings_noise += 1
            else:
                m.n_findings_unlabeled += 1

    # Drift detection: a topic that appeared at invocation N and
    # disappeared in N+1 is a drift alert (TODO §19 rationale —
    # "agent forgot a true finding").
    inv_indices = sorted(seen_topics_per_inv)
    for i, idx_curr in enumerate(inv_indices[1:], start=1):
        idx_prev = inv_indices[i - 1]
        gone = seen_topics_per_inv[idx_prev] - seen_topics_per_inv[idx_curr]
        for topic in gone:
            m.drift_alerts.append({
                "topic":           topic,
                "present_at":      idx_prev,
                "missing_at":      idx_curr,
            })

    # Rates.
    denom_validated = m.n_findings_valid + m.n_missed_total
    if denom_validated > 0:
        m.miss_rate = m.n_missed_total / denom_validated
    denom_blocker = m.n_findings_valid + m.n_missed_blocker
    if denom_blocker > 0:
        m.miss_rate_blocker = m.n_missed_blocker / denom_blocker
    if m.n_findings_total > 0:
        m.cumulative_noise_rate = m.n_findings_noise / m.n_findings_total

    return m


def _match_label_for_replay_comment(
    file_: str, line: int, replay_inv_idx: int,
    recorded_findings_by_invocation: dict[int, list[dict]],
    outcomes: Outcomes,
) -> Optional[CommentLabel]:
    """Find an outcomes.label for a replay-posted comment.

    Match heuristic: look at recorded postings for THIS invocation
    index first, then any invocation. Exact (file, line) match wins;
    file-only match falls back. No match → None.

    Returns the CommentLabel from outcomes.labels keyed by the
    matched recorded comment's stable_id, or None if either the
    finding has no recorded peer or no label exists.
    """
    # 1. Try same-invocation exact match.
    inv_candidates = recorded_findings_by_invocation.get(replay_inv_idx, [])
    for c in inv_candidates:
        if (c.get("file") == file_ and (c.get("line") or 0) == line):
            sid = c.get("stable_id")
            if sid and sid in outcomes.labels:
                return outcomes.labels[sid]
    # 2. Same invocation, file-only.
    for c in inv_candidates:
        if c.get("file") == file_:
            sid = c.get("stable_id")
            if sid and sid in outcomes.labels:
                return outcomes.labels[sid]
    # 3. Any invocation, exact.
    for cs in recorded_findings_by_invocation.values():
        for c in cs:
            if (c.get("file") == file_ and (c.get("line") or 0) == line):
                sid = c.get("stable_id")
                if sid and sid in outcomes.labels:
                    return outcomes.labels[sid]
    return None
