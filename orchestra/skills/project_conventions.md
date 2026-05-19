---
# Skill: project_conventions
#
# Pure-prose skill — no bundled tools. Teaches the agent to
# anchor judgments in the project's own conventions (docs,
# build manifests, framework idioms) BEFORE applying generic
# language-default reasoning. Earlier version was Java-centric;
# this one is language-agnostic and covers the doc / package-
# manager / framework triangle that defines "this project's
# normal" regardless of stack.
#
# Tools needed to act on this skill (`diff_read_file` to read the
# docs and manifests, optionally `jira_read_ticket` /
# `jira_dev_info` for tracker-side rules) come from OTHER skills
# the agent has already mounted — no need to re-declare them.
description: >-
  Anchor every code judgment in the project's own conventions
  before applying generic language / framework defaults. Cover
  the three sources of project truth: (1) explicit doc files
  (AGENTS.md / CONVENTIONS.md / CONTRIBUTING.md / README.md /
  docs/), (2) build + dependency manifests (the package manager
  + version-pinning files that define what the project IS),
  (3) framework / infra conventions (Spring / Django / Vue /
  Docker / k8s / Terraform / ...). Each layer's conventions
  override generic language defaults; cite the rule by source
  in finding evidence.
tools: []
---
## Project conventions — read what the project says about itself

Before drawing a conclusion that hinges on a domain rule, check
what the PROJECT'S own sources say. Generic language /
framework knowledge is a fallback; the project's own
conventions override it. Cite the rule by name when it bears on
a finding:

> "<SOURCE> says <RULE>, not <WHAT_THE_CODE_DOES>."
> *(substitute the real doc/manifest name, rule wording, and
> code snippet — generic placeholder shown here so the example
> doesn't leak benchmark-fixture content into the prompt.)*

Three sources to consult, in this priority order:

### 1. Explicit documentation files

The most authoritative source — somebody wrote these down on
purpose. Look for any of these at the repo root or under `docs/`:

- `AGENTS.md` — the canonical project-conventions doc, intended
  for code agents (this format). Reads as "rules of the road"
  for code in this repo.
- `CONVENTIONS.md` — same role under a different name (older
  projects).
- `CONTRIBUTING.md` — usually contributor-facing but often
  encodes review rules and code style.
- `README.md` — quickstart + architecture; sometimes carries
  norm-setting prose (logging conventions, module layout, error
  handling style).
- `docs/` (`docs/conventions.md`, `docs/architecture.md`, ADRs
  under `docs/adr/`) — long-form architecture decisions; an ADR
  explicitly saying "we use X not Y" overrides any generic
  preference.
- `CHANGELOG.md` — context for WHY a code path is the way it is
  ("changed in vX.Y for $reason").

Cite as `AGENTS.md says …` / `docs/adr/0042-async-defaults.md says …`.

### 2. Build + dependency manifests

The package manager / build system is the project's structural
declaration: it pins versions, declares features, gates plugins.
A mismatch between code and manifest is a finding on its own.

| Ecosystem | Manifest | Lockfile | Notes |
|---|---|---|---|
| **Node.js** | `package.json` | `package-lock.json` / `pnpm-lock.yaml` / `yarn.lock` | `engines:` pin runtime; `scripts:` define CI/dev contracts |
| **Python** | `pyproject.toml` / `setup.py` / `requirements.txt` | `poetry.lock` / `Pipfile.lock` / `uv.lock` | Tool config (ruff/mypy/black) often lives in pyproject |
| **JVM** | `pom.xml` (Maven), `build.gradle` / `build.gradle.kts` (Gradle) | (gradle's `gradle.lockfile`) | Spring Boot version pin lives here; multi-module structure declared in parent |
| **Go** | `go.mod` | `go.sum` | Module replace directives, toolchain version |
| **Rust** | `Cargo.toml` | `Cargo.lock` | Workspace structure, feature flags |
| **Ruby** | `Gemfile` | `Gemfile.lock` | Bundler + .ruby-version |
| **PHP** | `composer.json` | `composer.lock` | PSR autoload conventions declared here |
| **.NET** | `*.csproj` / `*.sln` | `packages.lock.json` | Target framework, NuGet refs |
| **C/C++** | `CMakeLists.txt` / `Makefile` / `conanfile.txt` / `vcpkg.json` | (varies) | Build flags + include paths often define ABI |

When a finding hinges on a library version, fetch coordinates
from the manifest, not from `import` statements. Lockfiles
matter for "is this dep actually pinned" questions.

### 3. Framework / infra conventions

Frameworks impose their own idioms; the project's choice OF
framework constrains what's idiomatic. The presence of these
files / patterns tells you what to grade against:

| Domain | Detect by | Conventions to anchor on |
|---|---|---|
| **Spring / Spring Boot** | `@SpringBootApplication`, `application.yml`, `pom.xml` with starter | Bean lifecycle, transactional boundaries, profile activation |
| **Django** | `manage.py`, `settings.py`, `apps.py` | App layout, migrations under `<app>/migrations/`, model-form-view triad |
| **FastAPI** | `from fastapi import …`, `app = FastAPI()` | Pydantic models, dependency-injection via `Depends()` |
| **React** | `package.json` with `react` dep, `.jsx` / `.tsx` files | Hooks rules (`use*` prefix, hook-order invariant), component file layout |
| **Vue** | `vue.config.*`, `.vue` files, `composables/` dir | Composition vs. options API choice (consistent within a codebase) |
| **Angular** | `angular.json`, decorator-heavy classes | Module/component/service boundaries enforced by CLI |
| **Docker** | `Dockerfile`, `.dockerignore` | Multi-stage builds, non-root USER, base-image pinning |
| **Docker Compose** | `docker-compose.yml`, `compose.yaml` | Service dependencies via `depends_on:`, healthchecks |
| **Kubernetes** | `*.yaml` with `apiVersion: apps/v1` shape | Resource limits, probes, labels-as-contract |
| **Helm** | `Chart.yaml`, `values.yaml`, `templates/` | Templating contract — never edit `templates/` to special-case one env, parameterize via values |
| **Terraform** | `*.tf`, `terraform.lock.hcl` | Module composition, state-as-source-of-truth |
| **Ansible** | `playbook.yml`, `roles/`, `inventory.ini` | Role boundaries, handler-vs-task split, idempotency requirement |
| **Make** | `Makefile` | Phony targets, the public contract for `make build` / `make test` |

If the project ships **`.editorconfig`**, **`.pre-commit-config.yaml`**,
**`.github/workflows/*.yml`**, or **`.github/CODEOWNERS`** — those
are direct, machine-enforced conventions. A finding that
contradicts what CI already enforces is either redundant (the
CI catches it) or the agent missed something (the CI passes
because the rule is narrower than the finding assumes); either
way the manifest is the tie-breaker.

### Priority and citation

When two sources disagree, prefer EXPLICIT documentation over
INFERRED framework norms over GENERIC language defaults. If
`AGENTS.md` says "use field injection in tests", that beats
Spring's general "prefer constructor injection" — cite
`AGENTS.md` in the finding's `evidence`. If `pyproject.toml`
pins `mypy = "1.4"`, don't grade against a `mypy 1.8` feature.

If NO project conventions are documented, the finding falls
back to generic-language reasoning. Say so plainly: "no
AGENTS.md / conventions doc found; applying generic <language>
norms — confirm with the team if this rule should be codified."
