"""
model_registry.py - Fetch available AI models from Anthropic and OpenAI APIs.

Returns a unified list with metadata:
  [{"id": "...", "provider": "anthropic"|"openai", "context_length": int|None,
    "status": "available"|"unavailable", "unavailable_reason": ""}]
Results are in-memory cached for CACHE_TTL seconds per process lifetime.
"""
import os
import time
from typing import Dict, List, Optional

import requests

CACHE_TTL = 300  # 5 minutes

_cache: Dict[str, object] = {}  # key -> (ts, value)


def _cached(key: str, ttl: int, fn):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < ttl:
        return entry[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


# ── Known context lengths (fallback when API doesn't return them) ────────────

_KNOWN_CONTEXT_LENGTHS: Dict[str, int] = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-haiku-4-5": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4-turbo": 128000,
    "gpt-4": 8192,
    "gpt-3.5-turbo": 16385,
    "o1": 200000,
    "o3": 200000,
}


def _lookup_context_length(model_id: str) -> Optional[int]:
    """Look up context length from known table, with prefix matching."""
    if model_id in _KNOWN_CONTEXT_LENGTHS:
        return _KNOWN_CONTEXT_LENGTHS[model_id]
    for prefix, length in _KNOWN_CONTEXT_LENGTHS.items():
        if model_id.startswith(prefix):
            return length
    return None


# ── Anthropic ──────────────────────────────────────────────────────────────────

_ANTHROPIC_PREFER = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]

def fetch_anthropic_models() -> List[Dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return _unavailable_anthropic_models("API key未配置")
    try:
        resp = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:
        return _unavailable_anthropic_models("请求失败: {}".format(str(exc)[:80]))

    models = []
    for m in data:
        mid = m.get("id", "")
        if not mid or "claude" not in mid.lower():
            continue
        ctx_len = _lookup_context_length(mid)
        models.append({
            "id": mid,
            "provider": "anthropic",
            "created": m.get("created_at", ""),
            "context_length": ctx_len,
            "status": "available",
            "unavailable_reason": "",
        })

    # Sort: preferred first, then by created_at desc
    def _sort_key(m):
        for i, p in enumerate(_ANTHROPIC_PREFER):
            if m["id"].startswith(p):
                return (0, i, "")
        return (1, 99, m["id"])

    models.sort(key=_sort_key)
    return models


def _unavailable_anthropic_models(reason: str) -> List[Dict]:
    """Return preferred Anthropic models marked as unavailable."""
    return [
        {
            "id": mid,
            "provider": "anthropic",
            "created": "",
            "context_length": _lookup_context_length(mid),
            "status": "unavailable",
            "unavailable_reason": reason,
        }
        for mid in _ANTHROPIC_PREFER
    ]


# ── OpenAI ────────────────────────────────────────────────────────────────────

_OPENAI_PREFIXES = ("gpt-4o", "gpt-4", "o1", "o3", "gpt-3.5-turbo")
_OPENAI_SKIP = ("instruct", "vision-preview", "0301", "0314", "0613")

_OPENAI_FALLBACK_MODELS = ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]

def fetch_openai_models() -> List[Dict]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _unavailable_openai_models("API key未配置")
    try:
        resp = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": "Bearer " + api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:
        return _unavailable_openai_models("请求失败: {}".format(str(exc)[:80]))

    models = []
    for m in data:
        mid = m.get("id", "")
        if not any(mid.startswith(p) for p in _OPENAI_PREFIXES):
            continue
        if any(s in mid for s in _OPENAI_SKIP):
            continue
        ctx_len = _lookup_context_length(mid)
        models.append({
            "id": mid,
            "provider": "openai",
            "created": m.get("created", 0),
            "context_length": ctx_len,
            "status": "available",
            "unavailable_reason": "",
        })

    models.sort(key=lambda m: -m["created"])
    return models


def _unavailable_openai_models(reason: str) -> List[Dict]:
    """Return common OpenAI models marked as unavailable."""
    return [
        {
            "id": mid,
            "provider": "openai",
            "created": 0,
            "context_length": _lookup_context_length(mid),
            "status": "unavailable",
            "unavailable_reason": reason,
        }
        for mid in _OPENAI_FALLBACK_MODELS
    ]


# ── Combined ──────────────────────────────────────────────────────────────────

def get_available_models(force_refresh: bool = False) -> List[Dict]:
    """Return unified model list from all configured providers."""
    if force_refresh:
        _cache.clear()

    def _fetch():
        result = []
        result.extend(fetch_anthropic_models())
        result.extend(fetch_openai_models())
        return result

    return _cached("all_models", CACHE_TTL, _fetch)


def make_label(m: Dict) -> str:
    """Short display label for a model entry."""
    provider_tag = "[C]" if m["provider"] == "anthropic" else "[O]"
    return "{} {}".format(provider_tag, m["id"])


def format_model_list_text(models: List[Dict]) -> str:
    """Format model list for Telegram display, grouped by provider."""
    if not models:
        return "(无可用模型)"

    # Group by provider
    grouped: Dict[str, List[Dict]] = {}
    for m in models:
        grouped.setdefault(m["provider"], []).append(m)

    # Sort within each group: available first, then unavailable
    for provider in grouped:
        grouped[provider].sort(key=lambda m: (0 if m.get("status") == "available" else 1, m["id"]))

    provider_labels = {"anthropic": "Anthropic", "openai": "OpenAI"}
    lines = []
    for provider in ["anthropic", "openai"]:
        provider_models = grouped.get(provider, [])
        if not provider_models:
            continue
        lines.append("── {} ──".format(provider_labels.get(provider, provider)))
        for m in provider_models:
            tag = "[C]" if provider == "anthropic" else "[O]"
            status = m.get("status", "available")
            ctx = m.get("context_length")
            if status == "available":
                ctx_str = " | {}K ctx".format(ctx // 1000) if ctx else ""
                lines.append("✅ {} {}{}".format(tag, m["id"], ctx_str))
            else:
                reason = m.get("unavailable_reason", "不可用")
                lines.append("⛔ {} {} | 不可用: {}".format(tag, m["id"], reason))
        lines.append("")

    return "\n".join(lines).strip()


def find_model(model_id: str, models: Optional[List[Dict]] = None) -> Optional[Dict]:
    """Find a model by ID in the model list. Returns None if not found."""
    if models is None:
        models = get_available_models()
    for m in models:
        if m["id"] == model_id:
            return m
    return None
