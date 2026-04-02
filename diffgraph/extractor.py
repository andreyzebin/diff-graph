from __future__ import annotations
import json
import logging
import re
from typing import Any, Callable, Optional

from .model import Module, Symbol
from .prompts import load as _load_prompt

OnEvent = Callable[..., None]  # on_event(event: str, **kwargs)

log = logging.getLogger(__name__)

EXTRACT_PROMPT = _load_prompt("extract_module.txt")
RETRY_SUFFIX = _load_prompt("extract_module_retry.txt")


def _numbered(content: str) -> str:
    """Prefix each line with its 1-based line number, e.g. '   1  import ...'."""
    lines = content.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i + 1:{width}}  {line}" for i, line in enumerate(lines))


def extract_module(
    path: str,
    content: str,
    lang: str,
    llm,
    model: str = "gpt-4o-mini",
    on_event: Optional[OnEvent] = None,
) -> Optional[Module]:
    """
    One LLM call → Module with symbols and dependencies.
    Retries up to 2 times on invalid JSON; returns None after 3 failures.
    """
    _emit = on_event or _noop
    base_prompt = EXTRACT_PROMPT.format(path=path, lang=lang, content=_numbered(content))
    prompt = base_prompt

    for attempt in range(3):
        _emit("extracting", path=path, model=model, attempt=attempt)
        try:
            raw = _call_llm(prompt, llm, model, on_event=on_event)
            data = _parse_llm_json(raw)
            module = _build_module(path, lang, data)
            _emit("extracted", path=path, symbols=len(module.symbols), deps=module.dependencies)
            return module
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("extract_module attempt %d/%d failed for %s: %s", attempt + 1, 3, path, exc)
            _emit("retry", path=path, attempt=attempt + 1, reason=str(exc))
            prompt = base_prompt + RETRY_SUFFIX

    log.warning("extract_module gave up after 3 attempts for %s", path)
    _emit("failed", path=path)
    return None


def _noop(*_, **__) -> None:
    pass


# ── internals ───────────────────────────────────────────────────────────────

def _call_llm(
    prompt: str,
    llm,
    model: str,
    on_event: Optional[OnEvent] = None,
) -> str:
    _emit = on_event or _noop
    stream = llm.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        stream=True,
    )
    chunks: list[str] = []
    for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        if delta:
            chunks.append(delta)
            _emit("token", text="".join(chunks))
    return "".join(chunks)


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Strip markdown fences and parse JSON."""
    text = raw.strip()
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    return json.loads(text)


def _build_module(path: str, lang: str, data: dict[str, Any]) -> Module:
    symbols = [
        Symbol(
            name=s["name"],
            kind=s.get("kind", "MODULE"),
            signature=s.get("signature", s["name"]),
            summary=s.get("summary", ""),
            annotations=s.get("annotations", []),
            start_line=int(s.get("start_line", 1)),
            end_line=int(s.get("end_line", 1)),
            calls=s.get("calls", []),
        )
        for s in data.get("symbols", [])
    ]
    return Module(
        id=path,
        name=data.get("name", _stem(path)),
        lang=lang,
        summary=data.get("summary", ""),
        symbols=symbols,
        dependencies=data.get("dependencies", []),
        depth=0,
    )


def _stem(path: str) -> str:
    import os
    return os.path.splitext(os.path.basename(path))[0]
