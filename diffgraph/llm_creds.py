"""
Provider profile loader for ~/.llm_creds.toml.

A profile is a flat dict of (api_url, api_key, model, tool_choice, timeout, ca_bundle, ...).
Strings are passed through ${VAR} env-var expansion at load time — secrets
never live in the file itself.

Lookup order (first hit wins):
  1. $LLM_CREDS_FILE
  2. .llm_creds.toml in cwd, walking up to filesystem root
  3. ~/.llm_creds.toml
  4. ~/repos/.llm_creds.toml
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Optional

_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    if env_path := os.environ.get("LLM_CREDS_FILE"):
        paths.append(Path(env_path).expanduser())
    cwd = Path.cwd().resolve()
    while True:
        paths.append(cwd / ".llm_creds.toml")
        if cwd == cwd.parent:
            break
        cwd = cwd.parent
    home = Path.home()
    paths.append(home / ".llm_creds.toml")
    paths.append(home / "repos" / ".llm_creds.toml")
    return paths


def find_creds_file() -> Optional[Path]:
    for p in _candidate_paths():
        if p.is_file():
            return p
    return None


def load_providers(path: Optional[Path] = None) -> dict[str, dict]:
    """Return {provider_name: profile_dict} with ${VAR} expanded."""
    src = path or find_creds_file()
    if src is None or not src.is_file():
        return {}
    with open(src, "rb") as f:
        data = tomllib.load(f)
    providers = data.get("providers", {})
    return {name: _expand(profile) for name, profile in providers.items()}


def get_provider(name: str, path: Optional[Path] = None) -> dict:
    """Return one provider profile or raise KeyError with a helpful message."""
    providers = load_providers(path)
    if name not in providers:
        available = ", ".join(sorted(providers)) or "(none)"
        src = path or find_creds_file() or "(no creds file found)"
        raise KeyError(
            f"provider '{name}' not in {src}. Available: {available}"
        )
    return providers[name]


def apply_provider(llm_cfg: dict, provider_name: str) -> dict:
    """
    Merge a provider profile into an existing llm_cfg dict.

    Profile keys map onto llm_cfg keys 1:1:
      base_url   -> api_url
      api_key    -> api_key
      model      -> model
      tool_choice, timeout, ca_bundle, headers   -> same name

    Non-empty values in the profile overwrite llm_cfg. Use CLI flags for
    further overrides (CLI > provider > config files).
    """
    profile = get_provider(provider_name)
    mapping = {
        "base_url": "api_url",
        "api_key": "api_key",
        "model": "model",
        "tool_choice": "tool_choice",
        "timeout": "timeout",
        "ca_bundle": "ca_bundle",
        "headers": "headers",
    }
    for src_key, dst_key in mapping.items():
        if src_key in profile and profile[src_key] not in ("", None):
            llm_cfg[dst_key] = profile[src_key]
    return llm_cfg
