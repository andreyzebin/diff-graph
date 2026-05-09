"""Auto-detect genes — discrete features inside a mutation.

A "gene" is a self-contained directive / capability / style choice
that a mutation either has or doesn't. Detection is marker-based:
each gene declares a substring pattern (or callable predicate) that
should be present in the compiled prompts iff the gene is active.

Detected genes get frozen on `runs.genes` at run start. Future
toggle-based mutations (5e.12 Phase 2) will populate the same
column from an explicit manifest instead.

Adding a new gene
-----------------
1. Pick a name (snake_case). Try to be specific — e.g.
   `severity_calibration_v2`, not just `severity`.
2. Add an entry to `GENES` below: either a string (substring match)
   or a callable that takes an `AgentConfig` and returns bool.
3. The marker should appear in the prompt verbatim when the feature
   is active. Avoid markers that match unrelated text.
"""
from __future__ import annotations

from typing import Callable, Iterable, Union

GeneMarker = Union[str, Callable[["AgentConfig"], bool]]  # noqa: F821


GENES: dict[str, GeneMarker] = {
    "diff_view_block":
        "## Diff view (how the file tools work)",
    "agents_md_citation_rule":
        "rule by name when it bears",
    "severity_calibration_v2":
        "follows consequence; verdict follows severity",
    "comment_graph_tools":
        "list_threads(start, n, sort)",
    "open_closed_user_message":
        "tools above are **capabilities**",
    "questions_remaining_concerns":
        "uses `questions_remaining` to express concerns",
    "react_emoji_lightweight_ack":
        "react_to_comment(comment_id, emoticon)",
    "post_comment_unified":
        "post_comment(text, file?, line?, severity?, parent_id?)",
    "set_review_status_explicit":
        "set_review_status(status, reason)",
    "spawn_agent_extension_point":
        "extension point",
    "no_baked_existing_comments":
        lambda agent: getattr(agent, "name", "") in ("dispatcher", "reviewer", "investigator")
                      and "existing_comments" not in (getattr(agent, "input_schema", {}) or {}),
    "diff_view_for_reviewer":
        lambda agent: getattr(agent, "name", "") == "reviewer"
                      and "## Diff view" in (getattr(agent, "system_prompt", "") or ""),
    "diff_view_for_investigator":
        lambda agent: getattr(agent, "name", "") == "investigator"
                      and "## Diff view" in (getattr(agent, "system_prompt", "") or ""),
    "look_only_thread_focus":
        "TRIGGER` inside the THREAD section",
}


def _agent_text(a) -> tuple[str, str]:
    """Extract (system_prompt, user_prompt) from an AgentRegistryEntry."""
    sys_p = (getattr(a, "system_prompt", None)
             or getattr(a, "prompt_template", None)
             or "") or ""
    usr_p = (getattr(a, "user_prompt", None)
             or getattr(a, "user_template", None)
             or "") or ""
    return sys_p, usr_p


def _matches(marker: GeneMarker, agents: Iterable) -> bool:
    """Return True if any agent satisfies the marker."""
    if callable(marker):
        try:
            return any(marker(a) for a in agents)
        except Exception:
            return False
    if not isinstance(marker, str):
        return False
    for a in agents:
        sys_p, usr_p = _agent_text(a)
        if marker in sys_p or marker in usr_p:
            return True
    return False


def detect_genes(agent_registry) -> list[str]:
    """Detect genes active in a compiled AgentRegistry.

    Returns sorted list of gene names. Empty list if no marker
    matched (e.g. minimal test prompts).

    Defensive — catches any per-gene exception and skips the gene
    rather than failing the run. Tracing must never be load-bearing.
    """
    if agent_registry is None:
        return []
    try:
        all_configs = agent_registry.get_all_configs()
        if isinstance(all_configs, dict):
            agents = list(all_configs.values())
        else:
            agents = list(all_configs or [])
    except Exception:
        return []
    out = []
    for name, marker in GENES.items():
        try:
            if _matches(marker, agents):
                out.append(name)
        except Exception:
            continue
    return sorted(out)
