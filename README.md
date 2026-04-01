# DiffGraph

Lightweight dependency metamodel for code-review agents.

Starts from a `git diff`, extracts entities with an LLM, and recursively walks
dependencies through the repository — producing a compact, structured context
that describes exactly the part of the codebase touched by a PR.

```
raw git diff
     │
     ▼
parse_diff() ──► changed files + changed lines + before snippets
     │
     ▼
explore()    ──► BFS: read file → LLM extract → resolve deps
     │
     ▼
MetaModel    ──► mark_changed_symbols() → before/after code
     │
     ▼
render()     ──► prompt context for a review agent
```

---

## Quickstart

### 1 — Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2 — Configure

Copy the config and set your API key:

```bash
cp config.yaml config.local.yaml
export OPENAI_API_KEY=sk-...
```

`config.local.yaml` is gitignored. Edit it to point at any OpenAI-compatible
endpoint (DeepSeek, Ollama, vLLM, etc.):

```yaml
llm:
  api_url: "https://api.deepseek.com/v1"
  api_key: "${DEEPSEEK_API_KEY}"
  model: "deepseek-chat"
```

### 3 — Run

```bash
# From a diff file
python cli.py run --repo ./my-service --diff changes.diff

# Pipe directly from git
git diff HEAD~1 | python cli.py run --repo . --diff -

# Save context to file instead of stdout
python cli.py run --repo . --diff my.diff --output context.txt

# Check what the diff parser sees (no LLM needed)
python cli.py inspect changes.diff
git diff HEAD~1 | python cli.py inspect -
```

---

## Configuration

Settings are loaded from `config.yaml`, with `config.local.yaml` merged on top.
All string values support `${ENV_VAR}` expansion.

```yaml
llm:
  api_url: ""               # empty = OpenAI directly
  api_key: "${OPENAI_API_KEY}"
  model: "gpt-4o-mini"

render:
  max_tokens: 8000          # rough token budget for the rendered context

explore:
  depth: 2                  # BFS depth: 0=changed files only, 1=+direct deps, 2=+transitive
```

Any option can also be overridden via CLI flags:

| Flag | Description |
|------|-------------|
| `--model` | LLM model name |
| `--depth` | BFS depth |
| `--api-url` | API base URL |
| `--api-key` | API key |
| `--output` | Write context to file |

---

## Python API

```python
from openai import OpenAI
from diffgraph import DiffGraph

client = OpenAI()  # or any OpenAI-compatible client
dg = DiffGraph(repo_path="./my-service", llm_client=client)

# Full pipeline: diff text → prompt context string
context = dg.build_and_render(open("my.diff").read(), depth=2)

# Step by step
meta, diff_result = dg.build(open("my.diff").read())
context = dg.render(meta, diff_result)
```

---

## Output format

```
## Changed Modules

### PaymentService.java [MODIFIED]
> "Handles payment transactions"

Modified symbols:
- [METHOD] public Order processPayment(OrderDTO dto) @Transactional
  > "Creates transaction, calls CardValidator"

  BEFORE:
  ```java
  public Order processPayment(OrderDTO dto) {
      cardValidator.validate(dto.getCard());
      return orderRepository.save(new Order(dto));
  }
  ```

  AFTER:
  ```java
  public Order processPayment(OrderDTO dto) {
      cardValidator.validate(dto.getCard());
      auditLog.record(dto);
      return orderRepository.save(new Order(dto));
  }
  ```

Other symbols:
- [METHOD] private void validateAmount(BigDecimal amount)
  > "Checks amount limits"

---

## Direct Dependencies (depth 1)

### CardValidator.java
> "Validates cards using Luhn algorithm"
- [METHOD] public boolean validate(CardDTO card)

---

## Transitive Dependencies (depth 2)

### CardDTO.java — "DTO for card data" [summary only]
```

---

## Architecture

```
diffgraph/
├── model.py        # Symbol, Module, MetaModel dataclasses
├── lang.py         # language detection, search patterns, file extensions
├── tools.py        # list_files, read_file, search_text
├── diff_parser.py  # git diff → DiffResult (hunks, changed lines)
├── extractor.py    # LLM extraction with 3-attempt retry
├── explorer.py     # BFS over dependencies
├── renderer.py     # text render with token-budget degradation
└── diffgraph.py    # DiffGraph public API + mark_changed_symbols
```

**Supported languages:** Java, Python, TypeScript/TSX, Go, Kotlin, Ruby, C#.

**Protections:**

| Mechanism | Behaviour |
|-----------|-----------|
| Token guard | `read_file` without range → first 300 lines |
| Max files | `list_files` > 50 results → first 10 |
| Visited set | Prevents cycles and duplicate LLM calls |
| LLM retry | Invalid JSON → up to 2 retries, then skip module |
| Token budget | `render()` degrades depth 2 → depth 1 → names only |
