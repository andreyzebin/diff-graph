"""
Streaming LLM helper for tool-calling agents.

Assembles a complete response from the SSE stream and fires a per-chunk
callback so callers can show live progress in the terminal.

Moved from diffgraph/streaming.py and generalized with **llm_params
passthrough for adaptive parameter control.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ── lightweight response types (drop-in for openai ChatCompletion) ────────────

@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _TC:
    id: str
    function: _Fn
    type: str = "function"


@dataclass
class _Msg:
    tool_calls: list  # list[_TC], empty = no tool calls (falsy)
    content: str = ""


@dataclass
class _Choice:
    message: _Msg


class StreamedResponse:
    """Compatible with openai.ChatCompletion for `.choices[0].message` and `.usage`."""
    def __init__(self, choices: list[_Choice], usage: Any):
        self.choices = choices
        self.usage = usage  # raw usage object from the stream's final chunk


# ── public API ────────────────────────────────────────────────────────────────

OnToken = Callable[[str, str, int], None]  # (tool_name, args_so_far, chunk_count)


def stream_llm(
    llm: Any,
    model: str,
    messages: list[dict],
    tools: list[dict],
    tool_choice: str | dict = "required",
    temperature: float = 0,
    on_token: Optional[OnToken] = None,
    stream: bool = True,
    extra_body: Optional[dict] = None,
    **llm_params: Any,
) -> StreamedResponse:
    """
    Call an LLM for tool-calling and assemble the full response.

    on_token(tool_name, args_so_far, chunk_count) is called for every streamed
    chunk so the caller can update a live display (only when stream=True).

    Extra **llm_params are passed through to the LLM API call (e.g.,
    top_p, frequency_penalty, presence_penalty, max_completion_tokens).

    `stream=False` falls back to non-streaming completion. Required for
    backends with broken streaming tool-call parsers (notably vLLM's
    Qwen3CoderToolParser, which can leak raw `</parameter>` XML
    fragments into JSON keys when streamed).

    `extra_body` is forwarded to the OpenAI client for vendor extensions
    like `chat_template_kwargs={"enable_thinking": False}` on Qwen3.

    Returns a StreamedResponse compatible with the non-streaming OpenAI interface.
    """
    # Build API kwargs — filter out None values
    create_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "temperature": temperature,
    }
    if stream:
        create_kwargs["stream"] = True
        create_kwargs["stream_options"] = {"include_usage": True}
    if extra_body:
        create_kwargs["extra_body"] = extra_body
    # Merge adaptive params (top_p, frequency_penalty, etc.)
    for k, v in llm_params.items():
        if v is not None:
            create_kwargs[k] = v

    if not stream:
        # Non-streaming path: response shape already matches what we return.
        resp = llm.chat.completions.create(**create_kwargs)
        msg = resp.choices[0].message
        tcs = []
        for tc in (msg.tool_calls or []):
            tcs.append(_TC(id=tc.id, function=_Fn(name=tc.function.name,
                                                   arguments=tc.function.arguments)))
        return StreamedResponse(
            choices=[_Choice(message=_Msg(tool_calls=tcs, content=msg.content or ""))],
            usage=getattr(resp, "usage", None),
        )

    tc_acc: dict[int, dict] = {}  # index -> {id, name, args}
    content_acc = ""
    usage = None
    chunk_count = 0
    stream_obj = llm.chat.completions.create(**create_kwargs)

    for chunk in stream_obj:
        # Final chunk carries usage when stream_options=include_usage
        if getattr(chunk, "usage", None):
            usage = chunk.usage

        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta
        chunk_count += 1

        if delta.content:
            content_acc += delta.content

        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tc_acc:
                    tc_acc[idx] = {"id": "", "name": "", "args": ""}
                if tc_delta.id:
                    tc_acc[idx]["id"] = tc_delta.id
                fn = tc_delta.function
                if fn:
                    if fn.name:
                        tc_acc[idx]["name"] = fn.name
                    if fn.arguments:
                        tc_acc[idx]["args"] += fn.arguments

                if on_token:
                    on_token(tc_acc[idx]["name"], tc_acc[idx]["args"], chunk_count)

    tool_calls = [
        _TC(id=v["id"], function=_Fn(name=v["name"], arguments=v["args"]))
        for _, v in sorted(tc_acc.items())
    ]
    msg = _Msg(tool_calls=tool_calls, content=content_acc)
    return StreamedResponse(choices=[_Choice(message=msg)], usage=usage)
