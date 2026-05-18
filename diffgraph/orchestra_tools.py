"""
Register diffgraph's domain-specific tools in an Orchestra ToolRegistry.

Each tool is a closure over the review context (_Ctx). Tools accept an
optional `ref` parameter for selecting the diff view:
  - "base..source" (default) — unified diff VFS
  - "<sha1>..<sha2>" — VFS for specific commit range
  - "source" — plain filesystem, no diff markers
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orchestra.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from .orchestrator import _Ctx

log = logging.getLogger(__name__)

_SKIP_DIRS = {
    ".venv", "venv", "env", "node_modules", "vendor",
    "dist", "build", ".git", "__pycache__", ".mypy_cache",
}


def _skip_dir(path: str) -> bool:
    return any(p in _SKIP_DIRS for p in path.replace("\\", "/").split("/"))


def _path_from_listing_row(row: str) -> str:
    """Extract the file path from a diff_list_files row.
    Row shape: `<X>  <path>  (<summary>)` — two spaces after status,
    two spaces before summary. We slice off the 3-char status prefix
    and trim trailing summary."""
    body = row[3:] if len(row) > 3 else row
    cut = body.rfind("  (")
    return (body[:cut] if cut > 0 else body).rstrip()


def _filter_and_page_listing(text: str, start: int, n: int) -> str:
    """Drop rows whose path is inside a skip-dir, then slice
    `[start : start+n]`. Path-width alignment from list_files_vfs is
    preserved — we only filter and slice, never re-align (column
    widths are sized once at format time, so trimming rows just
    leaves consistent trailing whitespace, which is fine in
    monospace). Appends a pagination footer when there are more rows
    than the page shows."""
    if not text:
        return ""
    rows = [r for r in text.splitlines()
            if not _skip_dir(_path_from_listing_row(r))]
    total = len(rows)
    page = rows[start : start + n]
    out = "\n".join(page)
    # Footer when there's more to see — agent knows to paginate.
    if total > start + n or start > 0:
        out += (
            f"\n\n[showing {start}..{start + len(page) - 1} of {total} — "
            f"call with start={start + n} to see the next page]"
        )
    return out


def _format_plain_listing(paths: list[str], repo_path: str) -> str:
    """Format a plain (non-VFS, `ref="source"`) listing with the same
    shape as list_files_vfs but no diff metadata — status is always
    space, summary is `(<L>L · <bytes>)`."""
    from diffsearch.tools import _fmt_bytes
    if not paths:
        return ""
    from pathlib import Path as _P
    rows: list[tuple[str, str]] = []
    for rel in paths:
        p = _P(repo_path) / rel
        try:
            size = p.stat().st_size
            with open(p, "rb") as f:
                L = sum(1 for _ in f)
        except (OSError, UnicodeDecodeError):
            size = 0
            L = 0
        rows.append((rel, f"({L}L · {_fmt_bytes(size)})"))
    path_w = max(len(r[0]) for r in rows)
    return "\n".join(f"   {path:<{path_w}}  {summary}"
                     for path, summary in rows)


def _resolve_ref(ctx: "_Ctx", ref: str) -> tuple[str, str] | None:
    """Resolve abstract ref names to SHA pair. Returns None for plain mode."""
    if ".." not in ref:
        return None  # plain mode (ref="source" or single SHA)
    left, right = ref.split("..", 1)
    left = ctx.base_ref if left == "base" else left
    right = ctx.source_ref if right == "source" else right
    if not left or not right:
        return None
    return left, right


def _get_vfs(ctx: "_Ctx", ref: str) -> str | None:
    """Get or create VFS directory for a ref range. Returns None for plain mode."""
    resolved = _resolve_ref(ctx, ref)
    if not resolved:
        return None

    # Cache key is the resolved SHA pair
    cache_key = f"{resolved[0]}..{resolved[1]}"
    if cache_key not in ctx.vfs_cache:
        from diffsearch.virtual_fs import materialize_vfs
        vfs_dir = materialize_vfs(ctx.repo_path, resolved[0], resolved[1])
        ctx.vfs_cache[cache_key] = vfs_dir
        log.info("VFS materialized for %s: %s", cache_key[:16], vfs_dir)
    return ctx.vfs_cache[cache_key]


def register_diffgraph_tools(registry: ToolRegistry, ctx: "_Ctx") -> None:
    """Register all diffgraph domain tools. Tools call ctx.ensure_repo() lazily."""
    from .tools import list_files as _list_files_impl, read_file, search_text
    from .outline import get_outline
    from .comment_tools import (
        pr_list_threads as _list_threads_impl,
        pr_read_thread as _read_thread_impl,
        pr_read_comment as _read_comment_impl,
        snapshot_max_id as _snapshot_max_id,
    )

    def _ensure():
        """Trigger lazy clone if needed."""
        ctx.ensure_repo()

    def _comment_snapshot() -> int:
        """Cache the run-start max_comment_id on ctx the first time
        a comment tool is called. After that the value is fixed —
        any pr_post_comment we make during the run gets a higher id and
        becomes invisible to these tools (consistent snapshot)."""
        v = getattr(ctx, "_comment_snapshot_max_id", None)
        if v is None:
            v = _snapshot_max_id(ctx.existing_comments or [])
            ctx._comment_snapshot_max_id = v
        return v

    def _bot_user() -> str:
        return getattr(ctx, "_bot_user", "") or ""

    def _subject_pattern():
        return getattr(ctx, "_subject_pattern", None)

    # ── Phase B: repo/pr parameter resolution (§10.4 + §10.6) ──────────
    # The `"default"` literal resolves to the current PR's URI / id from
    # ctx; an explicit URI matching ctx is tolerated; anything else
    # returns a clear Phase B message. Phase C wires real cross-repo
    # dispatch — see TODO §10.8.
    from .repo_uri import (
        ctx_repo_uri_from_pr_url as _uri_from_pr_url,
        ctx_pr_id_from_pr_url as _pr_id_from_pr_url,
        resolve_repo_param as _resolve_repo,
        resolve_pr_param as _resolve_pr,
    )

    def _ctx_repo_uri():
        """Current PR's repo URI, or None for fake/local runs whose
        pr_url doesn't carry Bitbucket's `/projects/<P>/repos/<R>/` shape."""
        return _uri_from_pr_url(getattr(ctx, "_pr_url", "") or "")

    def _ctx_pr_id():
        """Current PR's id as a string, or None when not extractable."""
        return _pr_id_from_pr_url(getattr(ctx, "_pr_url", "") or "")

    def _phase_b_gate(repo: str = "default", pr: str = "default"):
        """Resolve repo+pr params; return error string or None.

        `None` means proceed with existing in-context behaviour
        (default / matching). A non-None return is the Phase B
        rejection message — handler should surface it and stop.
        """
        _, err = _resolve_repo(repo, ctx_uri=_ctx_repo_uri())
        if err:
            return err
        _, err = _resolve_pr(pr, ctx_pr=_ctx_pr_id())
        if err:
            return err
        return None

    # ── Data provider: pr_context (cached, hidden) ────────────────────────

    def _resolve_jira_tickets() -> str:
        """Authoritative PR→ticket resolution via Bitbucket's
        Jira-integration endpoint. Returns a comma-joined list of
        `default/<project>/<key>` refs the reviewer copies verbatim
        into jira_read_ticket — or a short status string when there's
        nothing to resolve.

        Short-circuits for non-real PR URLs (unit-tier `fake://`,
        local `--repo` runs): those have no Bitbucket Jira link, and
        ticket-backed unit scenarios get their refs from the
        scenario's `agent_data.jira_tickets` instead. Any failure
        (no Application Link, network) degrades to '(unavailable)' —
        the reviewer just works from the diff."""
        pr_url = getattr(ctx, "_pr_url", "") or ""
        if not pr_url or pr_url.startswith("fake://"):
            return ""
        try:
            from .bitbucket import pr_jira_issues
            issues = pr_jira_issues(pr_url)
        except Exception:
            return "(unavailable)"
        refs = []
        for issue in issues:
            key = (issue.get("key") or "").strip()
            if not key:
                continue
            # handle/namespace/ticket_id — single-server `default`,
            # namespace = the Jira project (the key's prefix).
            project = key.split("-", 1)[0] if "-" in key else key
            refs.append(f"default/{project}/{key}")
        return ", ".join(refs) if refs else "(none)"

    @registry.register(
        name="pr",
        description="PR context data provider — lazy-fetched bundle of PR metadata.",
        cache=True,
    )
    def pr() -> dict:
        """Hidden data-provider exposed to prompt templates as
        `{{ pr.title }}` / `{{ pr.commits }}` / etc. via the
        `_HiddenToolProxy` in RunContext.to_kwargs. Not in the
        agent's tool surface — the LLM cannot call this.

        Lazy init: `_ensure()` triggers clone + fetch_pr on first
        access; subsequent template accesses hit the proxy's
        cached result. Templates that don't reference `{{ pr.* }}`
        pay zero cost (abstract / non-PR scenarios)."""
        _ensure()
        from .orchestrator import _get_commit_list, _format_existing_comments
        return {
            "title": getattr(ctx, "_pr_title", "") or "",
            "description": getattr(ctx, "_pr_description", "") or "",
            "existing_comments": _format_existing_comments(
                ctx.existing_comments,
                bot_user=getattr(ctx, '_bot_user', ''),
            ),
            "commits": _get_commit_list(ctx.repo_path, ctx.base_ref, ctx.source_ref)
                       if ctx.base_ref and ctx.source_ref else "(unavailable)",
            "jira_tickets": _resolve_jira_tickets(),
        }

    def _default_ref() -> str:
        """Compute default ref dynamically (after lazy init may have set base/source)."""
        return "base..source" if (ctx.base_ref and ctx.source_ref) else "source"

    # ── Test-only abstract tool: do_task ──────────────────────────────────
    # Deterministic doubler used by the SKILL-001 bench scenario (boss +
    # worker abstract delegation test). Real production agents never
    # have this in their tool surface; it's registered in the global
    # diffgraph tool catalogue so test prompts can reference it via
    # `tools: [do_task]`. No domain side-effects; pure function so the
    # judge can verify correctness deterministically.
    @registry.register(
        name="do_task",
        description=(
            "Compute the result of doubling an integer input. "
            "Returns the doubled value as a string. Deterministic, no "
            "side effects — used by abstract delegation tests where the "
            "actual computation matters less than which agent performs it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "input": {
                    "type": "integer",
                    "description": "The integer to double.",
                },
            },
            "required": ["input"],
        },
    )
    def do_task(input: int = 0) -> str:
        return str(int(input) * 2)

    # ── Test-only abstract tool: probe ────────────────────────────────────
    # Hidden-number search oracle for SKILL-002 (reflect-skill A/B). The
    # target is hardcoded so the test is fully deterministic and judges
    # can assert the final answer exactly. Optimal binary search finds
    # 73 in ≤ 7 probes; the scenario gives a budget of 10. Pure function,
    # no domain side-effects.
    _HIDDEN_TARGET = 73

    @registry.register(
        name="probe",
        description=(
            "Compare your guess against a hidden integer target in "
            "the inclusive range [1, 100]. Returns one of three "
            "verdicts:\n"
            "  - \"higher\" — the target is GREATER than your guess\n"
            "  - \"lower\"  — the target is LESS than your guess\n"
            "  - \"equal\"  — your guess IS the target\n"
            "Deterministic, no side effects — used by progressive-"
            "search tests where the agent must converge by binary "
            "search and report the final value via text_answer."
        ),
        parameters={
            "type": "object",
            "properties": {
                "guess": {
                    "type": "integer",
                    "description": "Your candidate value (1..100).",
                },
            },
            "required": ["guess"],
        },
    )
    def probe(guess: int = 0) -> str:
        g = int(guess)
        if g < _HIDDEN_TARGET:
            return "higher"
        if g > _HIDDEN_TARGET:
            return "lower"
        return "equal"

    # ── Test-only abstract tools: detective trio ──────────────────────────
    # whereabouts / evidence / motive — three orthogonal axes over a fixed
    # 4-suspect cast (alice, bob, carol, dave). Each tool returns a short
    # deterministic string per suspect, hardcoded to map onto one and only
    # one combinatorial answer:
    #
    #   only `bob` is simultaneously (a) without a verifiable alibi,
    #   (b) tied to physical evidence, and (c) carrying a motive.
    #
    # Used by SKILL-003 (detective) to stress-test the reflect skill:
    # the worker must call all 12 (4 suspects × 3 axes) tools and
    # accumulate the cross-axis state to deduce the murderer. Without
    # reflect's externalised state-banking, the chain length puts early
    # responses out of reach of working memory by the deduction step.
    _DETECTIVE_HIDDEN_MURDERER = "bob"
    _WHEREABOUTS = {
        "alice": "Library — confirmed by the librarian (signed in at 19:42, signed out at 21:15).",
        "bob":   "Home alone, no one to corroborate.",
        "carol": "At a party — at least eight other guests place her there the entire evening.",
        "dave":  "Hospital appointment — clinic records show check-in 19:30 and discharge 22:00.",
    }
    _EVIDENCE = {
        "alice": "No physical evidence places her at the scene.",
        "bob":   "A footprint at the scene matches his shoe size and tread pattern.",
        "carol": "No physical evidence places her at the scene.",
        "dave":  "No physical evidence places him at the scene.",
    }
    _MOTIVE = {
        "alice": "No known motive — relationship with the victim was neutral.",
        "bob":   "Argued publicly with the victim the day before the crime over an unpaid debt.",
        "carol": "Stands to inherit if the victim dies, but the will is several years old and she's not in financial need.",
        "dave":  "No known motive — barely knew the victim.",
    }

    def _normalise_name(raw: str) -> str:
        return str(raw or "").strip().lower()

    @registry.register(
        name="whereabouts",
        description=(
            "Look up where the named suspect claims to have been at "
            "the time of the crime, and whether the alibi has been "
            "independently corroborated. Deterministic per suspect; "
            "case-insensitive. Valid names: alice, bob, carol, dave."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Suspect name (alice / bob / carol / dave).",
                },
            },
            "required": ["name"],
        },
    )
    def whereabouts(name: str = "") -> str:
        return _WHEREABOUTS.get(_normalise_name(name),
                                "Unknown suspect.")

    @registry.register(
        name="evidence",
        description=(
            "Look up the physical evidence (or lack thereof) "
            "connecting the named suspect to the crime scene. "
            "Deterministic per suspect; case-insensitive."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Suspect name (alice / bob / carol / dave).",
                },
            },
            "required": ["name"],
        },
    )
    def evidence(name: str = "") -> str:
        return _EVIDENCE.get(_normalise_name(name),
                             "Unknown suspect.")

    @registry.register(
        name="motive",
        description=(
            "Look up the known motive (or lack thereof) the named "
            "suspect would have to harm the victim. Deterministic "
            "per suspect; case-insensitive."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Suspect name (alice / bob / carol / dave).",
                },
            },
            "required": ["name"],
        },
    )
    def motive(name: str = "") -> str:
        return _MOTIVE.get(_normalise_name(name),
                           "Unknown suspect.")

    # ── Test-only abstract tool: cluedo `suggest` ─────────────────────────
    # Single-agent Clue/Cluedo adaptation for SKILL-004 (reflect skill
    # proof-of-value on academically-validated multi-step deductive
    # reasoning — see arXiv 2603.17169, where multi-agent LLMs win only
    # 4/18 simulated games). Opponents are NOT LLMs here: they're
    # deterministic lookup tables baked into this tool. The agent under
    # test is the only LLM in the loop; opponents always reveal their
    # match deterministically (alphabetical-first if multiple) and the
    # envelope + hand distributions are hardcoded so the puzzle is
    # reproducible across runs.
    #
    # Universe (12 cards, 4+4+4 — reduced from the canonical 6+6+9 to
    # keep unit-runs fast while preserving the multi-step state-tracking
    # load that makes the task hard for LLMs):
    #
    #   Suspects: mustard, scarlet, plum, green
    #   Weapons : knife, rope, wrench, candlestick
    #   Rooms   : library, kitchen, ballroom, study
    #
    # Hardcoded distribution:
    #   Envelope    = mustard, knife, library
    #   Agent hand  = scarlet, kitchen, candlestick   (known to agent up-front)
    #   Opp_1 hand  = plum, rope, ballroom
    #   Opp_2 hand  = green, wrench, study
    #
    # suggest(s, w, r) walks opp_1 → opp_2 in fixed order, returns the
    # first match alphabetically (deterministic). If neither opponent
    # holds ANY of the three suggested cards AND none is in the agent's
    # own hand, the response is "no_disproof" — which uniquely identifies
    # those three cards as the envelope.
    _CLUE_ENVELOPE = {"suspect": "mustard", "weapon": "knife", "room": "library"}
    _CLUE_AGENT_HAND = {"scarlet", "kitchen", "candlestick"}
    _CLUE_OPPONENT_HANDS = [
        ("opp_1", {"plum", "rope", "ballroom"}),
        ("opp_2", {"green", "wrench", "study"}),
    ]

    @registry.register(
        name="suggest",
        description=(
            "Make a Cluedo suggestion: ask whether the trio "
            "(suspect, weapon, room) appears in any opponent's hand. "
            "The tool walks opponents in fixed order (opp_1, opp_2) "
            "and returns the FIRST match alphabetically as "
            "\"shown by <opp_N>: <card>\" (privately to you). If "
            "neither opponent holds ANY of the three cards, the "
            "response is \"no_disproof\" — which uniquely identifies "
            "those exact three as the envelope (i.e. the solution). "
            "Cards already in your own hand (scarlet / kitchen / "
            "candlestick) are NOT in the envelope by construction; "
            "if your suggestion contains them, opponents simply don't "
            "show them — so building all three suggestion slots out "
            "of your own hand is wasted information.\n"
            "Names are case-insensitive. Universe of cards:\n"
            "  Suspects: mustard, scarlet, plum, green\n"
            "  Weapons : knife, rope, wrench, candlestick\n"
            "  Rooms   : library, kitchen, ballroom, study"
        ),
        parameters={
            "type": "object",
            "properties": {
                "suspect": {
                    "type": "string",
                    "description": "Suspect name (mustard/scarlet/plum/green).",
                },
                "weapon": {
                    "type": "string",
                    "description": "Weapon name (knife/rope/wrench/candlestick).",
                },
                "room": {
                    "type": "string",
                    "description": "Room name (library/kitchen/ballroom/study).",
                },
            },
            "required": ["suspect", "weapon", "room"],
        },
    )
    def suggest(suspect: str = "", weapon: str = "", room: str = "") -> str:
        trio = {_normalise_name(suspect),
                _normalise_name(weapon),
                _normalise_name(room)}
        for opp_name, hand in _CLUE_OPPONENT_HANDS:
            intersection = trio & hand
            if intersection:
                # Alphabetical-first to keep the response stable across
                # tie-breaking changes in set iteration order.
                shown = sorted(intersection)[0]
                return f"shown by {opp_name}: {shown}"
        # Neither opponent has anything — but check whether one or more
        # of the suggested cards is in the AGENT's own hand. The
        # academic Clue rule says nobody (including the asker) reveals
        # in that case, so we just return no_disproof — but it would be
        # wrong to conclude "envelope!" because the agent's own card
        # could be silencing the suggestion. Add an explicit hint so
        # the model learns not to confuse the cases.
        own_overlap = trio & _CLUE_AGENT_HAND
        if own_overlap:
            return (
                f"no_disproof — but you suggested {len(own_overlap)} "
                f"card(s) from your own hand ({sorted(own_overlap)}); "
                f"those cards can't be in the envelope by construction, "
                f"so this is not yet a solution. Try a suggestion built "
                f"from cards you have NOT seen anywhere."
            )
        return "no_disproof"

    @registry.register(
        name="diff_list_files",
        description=(
            "List paths in the diff view (default `ref=base..source`) "
            "as plain text, one row per file with a status code and a "
            "size summary so you can budget reads before calling "
            "`diff_read_file`. Example rows:\n"
            "    M  src/OrderService.java  (68L · +12/-8 · 2.4kB)\n"
            "    A  src/AuditLog.java      (50L · +50/-0 · 1.8kB)\n"
            "    D  src/LegacyService.java (30L · +0/-30 · 1.1kB)\n"
            "       src/Util.java          (42L · 980B)\n"
            "\n"
            "Status: `A`/`M`/`D`/`R`/`C`/`T` (added/modified/deleted/"
            "renamed/copied/type-changed) or a space for unchanged "
            "context files — same alphabet as `git diff --name-status`. "
            "Renames surface as a pair: `D <old>` + `R <new>`. "
            "Summary: `L` = total lines in the unified-diff view "
            "(= what `diff_read_file(path)` returns without a range); "
            "`+N/-M` = added/removed line counts (omitted for "
            "unchanged files, where both are zero); bytes = file size. "
            "\n\n"
            "Paginated: `start` (offset) + `n` (page size, default 50). "
            "When more rows exist than fit on the page a footer lists "
            "the total and the `start` value for the next call. "
            "`changes_only=true` (default) drops unchanged context "
            "files — call with `changes_only=false` to also see the "
            "unchanged surroundings."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob, e.g. '**/*.java'. Default '**/*' = everything — call with no args to list every file."},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
                "changes_only": {"type": "boolean", "description": "Default true — only files with status M/A/D/R/C/T. Set false to also include unchanged context files."},
                "start": {"type": "integer", "description": "Pagination offset (default 0)."},
                "n": {"type": "integer", "description": "Page size (default 50)."},
                "repo": {"type": "string", "description": 'Repo URI "bitbucket://<handle>/<project>/<repo>" or "default" (the current PR\'s repo). Defaults to "default"; in this release leave as default.'},
            },
            # Nothing is required — every field has a sensible default.
            # Marking pattern as required earlier caused tool-schema
            # validation to reject the typical opening call
            # `diff_list_files()` and blocked reviewers that always
            # orient themselves with an unfiltered first listing.
            "required": [],
        },
    )
    def diff_list_files(
        pattern: str = "**/*",
        ref: str = "",
        changes_only: bool = True,
        start: int = 0,
        n: int = 50,
        repo: str = "default",
    ) -> str:
        err = _phase_b_gate(repo=repo)
        if err:
            return err
        _ensure()
        ref = ref or _default_ref()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import list_files_vfs
            text = list_files_vfs(vfs_dir, pattern, changes_only=changes_only)
            return _filter_and_page_listing(text, start=start, n=n)
        # Plain mode (no VFS, no diff metadata) — no status info, so
        # `changes_only` is a no-op here. Paginate against the raw list.
        files = _list_files_impl(pattern, ctx.repo_path)
        files = [f for f in files if not _skip_dir(f)]
        total = len(files)
        page = files[start : start + n]
        out = _format_plain_listing(page, ctx.repo_path)
        if total > start + n or start > 0:
            out += (
                f"\n\n[showing {start}..{start + len(page) - 1} of {total} — "
                f"call with start={start + n} to see the next page]"
            )
        return out

    @registry.register(
        name="diff_read_file",
        description=(
            "Read a file from the diff view as unified diff: every line "
            "carries a `+`/`-`/` ` marker plus its old (base) and new "
            "(source) line numbers. Without a range, returns the full "
            "file annotated. With `changes_only=true`, returns only the "
            "changed hunks ±`before`/`after` context lines. With "
            "`ref=\"source\"`, returns plain file content without markers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "L position (1-indexed, inclusive). L = position in the unified-diff view, as shown by diff_outline."},
                "end_line": {"type": "integer", "description": "L position (1-indexed, inclusive)."},
                "changes_only": {"type": "boolean", "description": "Collapse output to changed lines with ±context."},
                "before": {"type": "integer", "description": "Context lines before each hunk when changes_only=true (default 3)."},
                "after": {"type": "integer", "description": "Context lines after each hunk when changes_only=true (default 3)."},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
                "repo": {"type": "string", "description": 'Repo URI "bitbucket://<handle>/<project>/<repo>" or "default" (the current PR\'s repo). Defaults to "default"; in this release leave as default.'},
            },
            "required": ["path"],
        },
    )
    def read_file_tool(path: str = "", start_line: int = None, end_line: int = None,
                       changes_only: bool = False, before: int = 3, after: int = 3,
                       ref: str = "", repo: str = "default") -> str:
        err = _phase_b_gate(repo=repo)
        if err:
            return err
        _ensure()
        ref = ref or _default_ref()
        if not changes_only and start_line is not None and end_line is not None and (end_line - start_line) > 100:
            end_line = start_line + 99
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import read_file_vfs
            return read_file_vfs(
                vfs_dir, path,
                start_line=start_line or 1,
                end_line=end_line,
                changes_only=changes_only,
                context_before=before,
                context_after=after,
            )
        return read_file(path, ctx.repo_path, start_line, end_line) or "(file not found)"

    @registry.register(
        name="diff_outline",
        description=(
            "Structural outline (classes, methods, fields) of a file in "
            "the diff view. Each symbol shows L range (use for diff_read_file "
            "ranges) and old/new ranges (for reference). Changed symbols "
            "are marked `*`; for changed methods the outline shows "
            "separate `Lold:..` and `Lnew:..` so you can read the old or "
            "new version individually."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
                "repo": {"type": "string", "description": 'Repo URI "bitbucket://<handle>/<project>/<repo>" or "default" (the current PR\'s repo). Defaults to "default"; in this release leave as default.'},
            },
            "required": ["path"],
        },
    )
    def read_outline_tool(path: str = "", ref: str = "", repo: str = "default") -> str:
        err = _phase_b_gate(repo=repo)
        if err:
            return err
        _ensure()
        ref = ref or _default_ref()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import read_outline_vfs
            return read_outline_vfs(vfs_dir, path, repo_path=ctx.repo_path)
        fd = ctx.diff_result.files.get(path)
        changed = set(fd.after_changed_lines) if fd else None
        return get_outline(path, ctx.repo_path, changed)

    @registry.register(
        name="diff_search",
        description=(
            "Search a string or regex across files in the diff view. Each "
            "hit is returned with its `+`/`-`/` ` marker and L/old/new "
            "coordinates — you see added, deleted, and unchanged "
            "occurrences in one query. Results are grouped by file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "glob": {"type": "string", "description": "File filter, e.g. '**/*.java'. Default '**/*'."},
                "regex": {"type": "boolean", "description": "Treat query as regex."},
                "before": {"type": "integer", "description": "Context lines before each match (default 0)."},
                "after": {"type": "integer", "description": "Context lines after each match (default 0)."},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
                "repo": {"type": "string", "description": 'Repo URI "bitbucket://<handle>/<project>/<repo>" or "default" (the current PR\'s repo). Defaults to "default"; in this release leave as default.'},
            },
            "required": ["query"],
        },
    )
    def search_tool(query: str = "", glob: str = "**/*", regex: bool = False,
                    before: int = 0, after: int = 0, ref: str = "",
                    repo: str = "default") -> str:
        err = _phase_b_gate(repo=repo)
        if err:
            return err
        _ensure()
        ref = ref or _default_ref()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import search_vfs
            return search_vfs(
                vfs_dir, query, glob=glob, regex=regex,
                before=before, after=after,
            )
        results = search_text(query, ctx.repo_path, glob=glob, regex=regex)
        filtered = [r for r in results if not _skip_dir(r.file)][:30]
        if not filtered:
            return "(no matches)"
        lines = []
        for r in filtered:
            lines.append(f"{r.file}:{r.line}: {r.text}")
        return "\n".join(lines)

    # ── Comment-graph navigation (replaces baked EXISTING COMMENTS) ──────

    @registry.register(
        name="pr_list_threads",
        description=(
            "List the PR's root comment threads — orientation across the "
            "discussion. Each row is one line with id, author, reply count, "
            "and the first line of the root body. Snapshot at run start: "
            "comments posted by the agent itself during the run are not "
            "shown here. Use `pr_read_thread(id)` to drill into a specific one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "start": {"type": "integer", "description": "Pagination offset (default 0)."},
                "n": {"type": "integer", "description": "Page size (default 30)."},
                "sort": {
                    "type": "string",
                    "enum": ["newest", "most_active", "oldest"],
                    "description": "Order. 'newest' (default) = recent root first; 'most_active' = most replies; 'oldest' = chronological.",
                },
                "repo": {"type": "string", "description": 'Repo URI or "default" (current PR\'s repo). Defaults to "default".'},
                "pr": {"type": "string", "description": 'PR id (string) or "default" (current PR). Defaults to "default".'},
            },
            "required": [],
        },
    )
    def list_threads_tool(start: int = 0, n: int = 30, sort: str = "newest",
                          repo: str = "default", pr: str = "default") -> str:
        err = _phase_b_gate(repo=repo, pr=pr)
        if err:
            return err
        return _list_threads_impl(
            ctx.existing_comments or [],
            snapshot_max_id_value=_comment_snapshot(),
            bot_user=_bot_user(),
            subject_pattern=_subject_pattern(),
            start=start,
            n=n,
            sort=sort,
        )

    @registry.register(
        name="pr_read_thread",
        description=(
            "Render the FULL thread containing the given comment, depth-first "
            "from the root. Pass any comment id — root, leaf, or middle of the "
            "tree; the tool finds the root and walks the subtree. Each comment "
            "appears as a header block (`=== #id by author · reply to #X · …`) "
            "followed by its verbatim body (markdown / code blocks preserved). "
            "Long bodies and deep trees are truncated with explicit hints to "
            "call `pr_read_comment(id)` or `pr_read_thread(<sub_id>)` to expand."
        ),
        parameters={
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer", "description": "Any comment id in the thread of interest."},
                "repo": {"type": "string", "description": 'Repo URI or "default" (current PR\'s repo). Defaults to "default".'},
                "pr": {"type": "string", "description": 'PR id (string) or "default" (current PR). Defaults to "default".'},
            },
            "required": ["comment_id"],
        },
    )
    def read_thread_tool(comment_id: int = 0,
                         repo: str = "default", pr: str = "default") -> str:
        err = _phase_b_gate(repo=repo, pr=pr)
        if err:
            return err
        if not comment_id:
            return "(comment_id is required)"
        return _read_thread_impl(
            ctx.existing_comments or [],
            comment_id=comment_id,
            snapshot_max_id_value=_comment_snapshot(),
            bot_user=_bot_user(),
            subject_pattern=_subject_pattern(),
        )

    @registry.register(
        name="pr_read_comment",
        description=(
            "Render ONE specific comment in full, no caps. Use when "
            "`pr_read_thread` truncated a body and you need the rest."
        ),
        parameters={
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer"},
                "repo": {"type": "string", "description": 'Repo URI or "default" (current PR\'s repo). Defaults to "default".'},
                "pr": {"type": "string", "description": 'PR id (string) or "default" (current PR). Defaults to "default".'},
            },
            "required": ["comment_id"],
        },
    )
    def read_comment_tool(comment_id: int = 0,
                          repo: str = "default", pr: str = "default") -> str:
        err = _phase_b_gate(repo=repo, pr=pr)
        if err:
            return err
        if not comment_id:
            return "(comment_id is required)"
        return _read_comment_impl(
            ctx.existing_comments or [],
            comment_id=comment_id,
            snapshot_max_id_value=_comment_snapshot(),
            bot_user=_bot_user(),
            subject_pattern=_subject_pattern(),
        )

    # ── Cross-repo discovery + PR resource (§10.2, Phase B plumbing) ──
    # In Phase B these tools return only the current PR / repo scope;
    # non-default URIs hit the `_phase_b_gate` and surface a clear
    # "Phase C" message. Phase C scenarios drive the real Bitbucket API
    # enumeration these tools will dispatch to.

    @registry.register(
        name="pr_get",
        description=(
            "Read a PR's coordinates — meta + base/source SHA + state + "
            "author. For the current PR these values are pre-filled in "
            "context; `pr_get` exposes the same data through the tool "
            "surface (and, in Phase C, resolves it for OTHER PRs in "
            "other repos)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": 'Repo URI "bitbucket://<handle>/<project>/<repo>" or "default" (current PR\'s repo).'},
                "pr": {"type": "string", "description": 'PR id (string) or "default" (current PR).'},
            },
            "required": [],
        },
    )
    def pr_get_tool(repo: str = "default", pr: str = "default") -> dict:
        err = _phase_b_gate(repo=repo, pr=pr)
        if err:
            return {"error": err}
        uri = _ctx_repo_uri()
        return {
            "uri": str(uri) if uri else "",
            "pr": _ctx_pr_id() or "",
            "base_ref": ctx.base_ref or "",
            "source_ref": ctx.source_ref or "",
            "pr_url": getattr(ctx, "_pr_url", "") or "",
        }

    @registry.register(
        name="pr_list",
        description=(
            "List PRs at the given URI level — server / project / repo. "
            "Phase B placeholder: returns only the current PR. Phase C "
            "scenarios drive the real Bitbucket API enumeration (cross-"
            "repo / cross-project PR feeds, token-scoped — no manifest "
            "allowlist, see §10.4)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": 'Repo URI (any level: handle / handle/project / handle/project/repo) or "default" (current repo).'},
            },
            "required": [],
        },
    )
    def pr_list_tool(repo: str = "default") -> list:
        err = _phase_b_gate(repo=repo)
        if err:
            return [{"error": err}]
        uri = _ctx_repo_uri()
        pr = _ctx_pr_id()
        if not uri or not pr:
            return []
        return [{"uri": str(uri), "pr": pr}]

    @registry.register(
        name="repo_list",
        description=(
            "List repos at the given URI level. With no arg (or "
            '"default"), lists repos at server level for the current '
            "handle. Phase B placeholder: returns only the current "
            "repo. Phase C scenarios drive the real Bitbucket API "
            "enumeration (token-scoped, no manifest allowlist)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": 'Repo URI (any level) or "default" — server-level for the current handle.'},
            },
            "required": [],
        },
    )
    def repo_list_tool(repo: str = "default") -> list:
        err = _phase_b_gate(repo=repo)
        if err:
            return [{"error": err}]
        uri = _ctx_repo_uri()
        if not uri:
            return []
        return [{"uri": str(uri)}]

    # One JiraRegistry per tool-registration — loads jira_servers:
    # from config.local.yaml once; per-handle JiraProvider instances
    # are lazy (network client built on first use) and cached.
    _jira_registry = None

    def _jira():
        nonlocal _jira_registry
        if _jira_registry is None:
            from .providers.jira import JiraRegistry
            _jira_registry = JiraRegistry.from_config()
        return _jira_registry

    @registry.register(
        name="jira_read_ticket",
        description=(
            "Read the issue tracker ticket a PR claims to address — "
            "its summary, type, status, description / acceptance "
            "criteria, recent comments, status history, and linked "
            "issues. `ref` is copied verbatim from the PR's "
            "`jira_tickets` list (shape `handle/namespace/ticket_id`); "
            "a bare ticket key also works. Linked issues come back "
            "inline — call `jira_read_ticket` again on a linked key to go "
            "deeper. If the ticket can't be reached (not found, Jira "
            "not configured) the tool says so plainly — proceed with "
            "the diff + PR description alone, don't retry in a loop."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": (
                        "Ticket reference — `handle/namespace/ticket_id` "
                        "as given in the PR's jira_tickets list, or a "
                        "bare ticket key (e.g. ORD-301)."
                    ),
                },
            },
            "required": ["ref"],
        },
    )
    def read_ticket_tool(ref: str = "") -> str:
        ref = (ref or "").strip()
        if not ref:
            return "(ref is required)"
        # The registry parses `handle/namespace/ticket_id`, routes to
        # the right server (or the env-based `default`), and fetches.
        # A bare key works too — parse_ticket_ref defaults the handle.
        try:
            from .providers.jira import format_ticket
            return format_ticket(_jira().fetch(ref))
        except Exception as exc:  # never crash the agent on a tracker hiccup
            return (
                f"(jira_read_ticket failed for {ref!r}: {type(exc).__name__}: "
                f"{exc} — proceed with the diff + PR description alone)"
            )

    @registry.register(
        name="pr_post_comment",
        description=(
            "Post a single comment on the PR. One tool covers all three "
            "shapes that share Bitbucket's comments endpoint:\n"
            "- general comment: just `text`.\n"
            "- inline finding: `text` + `file` + `line` (+ optional `severity`).\n"
            "- reply to existing thread: `text` + `parent_id`.\n"
            "Severity is one of BLOCKER / MAJOR / MINOR / COMMENT and only "
            "matters for inline comments. Returns the posted comment id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "file": {"type": "string", "description": "Relative path; pair with `line` for an inline anchor."},
                "line": {"type": "integer", "description": "1-indexed line in the changed (right) version."},
                "severity": {
                    "type": "string",
                    "enum": ["BLOCKER", "MAJOR", "MINOR", "COMMENT"],
                    "description": "Only meaningful for inline comments.",
                },
                "parent_id": {"type": "integer", "description": "Comment id to reply under."},
                "repo": {"type": "string", "description": 'Repo URI or "default" (current PR\'s repo). Defaults to "default".'},
                "pr": {"type": "string", "description": 'PR id (string) or "default" (current PR). Defaults to "default".'},
            },
            "required": ["text"],
        },
    )
    def post_comment_tool(text: str = "",
                          file: str = "",
                          line: int = 0,
                          severity: str = "",
                          parent_id: int = 0,
                          repo: str = "default",
                          pr: str = "default") -> dict:
        err = _phase_b_gate(repo=repo, pr=pr)
        if err:
            return {"status": "error", "message": err}
        pr_url = getattr(ctx, "_pr_url", "") or ""
        if not pr_url:
            return {"status": "skipped",
                    "message": "no pr_url on ctx; running locally — comments not posted"}
        if not text or not text.strip():
            return {"status": "error", "message": "text is required"}

        decorate = getattr(ctx, "_decorate", None)
        author_prefix = getattr(ctx, "_author_prefix", "") or ""
        body = text
        if author_prefix and not body.lstrip().startswith(author_prefix):
            body = f"{author_prefix} {body}"
        if decorate:
            body = decorate(body)

        # Best-effort line snap when we have a diff result and the agent
        # picked a line that's near (but not exactly on) a changed line.
        snapped_line = int(line or 0)
        if file and snapped_line and ctx._initialized and ctx.diff_result is not None:
            fd = ctx.diff_result.files.get(file)
            if fd is not None:
                changed = fd.after_changed_lines
                if changed and snapped_line not in changed:
                    snapped_line = min(changed, key=lambda L: abs(L - snapped_line))

        from diffgraph.bitbucket import post_pr_comment
        try:
            cid = post_pr_comment(
                pr_url,
                text=body,
                file=file or "",
                line=snapped_line,
                severity=severity or "",
                parent_id=int(parent_id or 0),
            )
        except Exception as exc:
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

        mode = "reply" if parent_id else ("inline" if (file and line) else "general")
        ctx.review_context.posted_comments.append({
            "comment_id": cid, "mode": mode,
            "file": file, "line": snapped_line,
            "parent_id": int(parent_id or 0),
        })
        return {"status": "posted", "comment_id": cid, "mode": mode}

    @registry.register(
        name="set_review_status",
        description=(
            "Set the agent's overall verdict on the PR. Use APPROVED when no "
            "blocking issues are present, NEEDS_WORK when at least one "
            "BLOCKER or MAJOR finding stands, UNAPPROVED to clear a prior "
            "decision. Strictness — when to approve vs request changes — is "
            "guided by the agent prompt, not this tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["APPROVED", "NEEDS_WORK", "UNAPPROVED"],
                },
                "reason": {
                    "type": "string",
                    "description": "One-sentence explanation, recorded for audit.",
                },
            },
            "required": ["status"],
        },
    )
    def set_review_status_tool(status: str = "", reason: str = "") -> dict:
        normalised = (status or "").strip().upper()
        if normalised not in ("APPROVED", "NEEDS_WORK", "UNAPPROVED"):
            return {"status": "error",
                    "message": f"unknown status {status!r}; expected APPROVED / NEEDS_WORK / UNAPPROVED"}
        ctx.review_context.review_status = normalised
        ctx.review_context.review_status_reason = reason or ""
        return {"status": "queued", "review_status": normalised}
