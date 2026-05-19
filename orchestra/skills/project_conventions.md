---
# Skill: project_conventions
#
# Pure-prose skill — no bundled tools. The text was duplicated
# word-for-word in reviewer.system.md and investigator.system.md
# as "## Project conventions"; the diff_view-style migration
# (one source of truth, mounted via skills:) keeps both prompts
# in lockstep when the rule wording evolves.
#
# Tools needed to act on this skill (`diff_read_file` for
# AGENTS.md, jira_read_ticket for tracker-side rules) come from
# OTHER skills the agent has already mounted — no need to
# re-declare them here.
description: >-
  The "check for a project-conventions doc before drawing
  conclusions" pattern. Spells out which filenames count
  (AGENTS.md / CONVENTIONS.md / CONTRIBUTING.md / docs/
  conventions.md), why project rules override generic
  framework/language defaults, and how to cite the rule by name
  in a finding's evidence.
tools: []
---
## Project conventions

Before drawing a conclusion that hinges on a domain rule, check the
repo for a project-conventions doc — typically `AGENTS.md` at the
repo root, sometimes `CONVENTIONS.md`, `CONTRIBUTING.md`, or
`docs/conventions.md`. The project's own convention overrides
generic Java / JPA / Spring / language-default reasoning. Cite the
rule by name when it bears on a finding:

> "<CONVENTIONS_DOC> says <RULE>, not <WHAT_THE_CODE_DOES>."
> *(substitute the real doc name, rule wording, and code snippet
> from the diff — generic placeholder shown here so the example
> doesn't leak any benchmark-fixture content into the prompt.)*
