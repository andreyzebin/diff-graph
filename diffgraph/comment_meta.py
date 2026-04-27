"""
Traceability tag for PR comments.

Single source of truth for the metadata footer that gets appended to every
comment DiffGraph posts on a PR. Lives outside the bitbucket transport so the
transport layer stays dumb (text in → HTTP out) and only one decision point
controls whether/what to tag.

Footer format:  `{prefix}:{generation}:{mutation}:{run}`
e.g.            `dg:prompts-v2:f7917d6:da090cf2-...`
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommentMeta:
    prefix: str
    generation: str
    mutation: str
    run: str

    def decorate(self, text: str) -> str:
        return f"{text}\n\n`{self.prefix}:{self.generation}:{self.mutation}:{self.run}`"


def build_comment_meta(
    *,
    prompt_source: str,
    prompt_hash: str,
    run_id: str,
    prefix: str = "dg",
) -> CommentMeta | None:
    """
    Build the meta tag, or return None when tagging is disabled.

    prefix=""  → tagging disabled (e.g. opt-out via CLI flag / YAML)
    Empty prompt_source AND empty prompt_hash → nothing meaningful to tag
    """
    if not prefix:
        return None
    if not (prompt_source or prompt_hash):
        return None
    return CommentMeta(
        prefix=prefix,
        generation=_derive_generation(prompt_source),
        mutation=prompt_hash[:7] if prompt_hash else "",
        run=run_id,
    )


def _derive_generation(prompt_source: str) -> str:
    """Last meaningful path segment; skip a trailing `prompts` dir."""
    if not prompt_source:
        return "default"
    parts = prompt_source.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-1] == "prompts":
        return parts[-2]
    return parts[-1]
