"""Turn an API profile (base URL + key + protocol + model) into a per-session launch spec for a provider CLI.

The spec only ever touches ONE session's environment/arguments - the user's own global provider config is never edited.
Mechanisms come from each tool's documentation (see PLAN-v3.md); where a tool has no per-session mechanism we say so
instead of pretending.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional



class ProfileError(ValueError):
    """The chosen profile cannot be used with this provider (reason is shown to the user)."""


# provider id -> (protocols it can speak to a custom endpoint, human note)
SUPPORT: Dict[str, Dict[str, Any]] = {
    "claude": {"protocols": ("anthropic",), "note": "needs an Anthropic Messages-compatible endpoint"},
    "codex": {"protocols": ("openai-responses",), "note": "needs an endpoint that implements the OpenAI Responses API"},
    "opencode": {"protocols": ("openai-chat", "openai-responses"), "note": "works with OpenAI-compatible endpoints"},
}
UNSUPPORTED = {
    "hermes": "Hermes only reads its endpoint from ~/.hermes/config.yaml (run `hermes model` on that machine), so it cannot be set per session",
    "agy": "Antigravity has no custom-endpoint setting",
    "shell": "a plain shell does not call a model",
    "opencode-free": "this variant is pinned to OpenCode's free models; use the OpenCode provider with a profile instead",
}
# Node-side allowlist (defence in depth): only these env names / flags may be injected into a session.
ENV_ALLOW = re.compile(r"^(ANTHROPIC_(BASE_URL|AUTH_TOKEN|MODEL|SMALL_FAST_MODEL)|OPENCODE_CONFIG_CONTENT|KODE_API_KEY)$")
ARG_FLAGS = ("-c", "-m", "--model")


def supported(provider: str) -> bool:
    return provider in SUPPORT


def launch_spec(provider: str, profile: Dict[str, Any], model: str = "") -> Dict[str, Any]:
    """Return {"env": {...}, "args": [...], "secrets": [key]} for one session, or raise ProfileError."""
    if provider in UNSUPPORTED:
        raise ProfileError(UNSUPPORTED[provider])
    sup = SUPPORT.get(provider)
    if not sup:
        raise ProfileError(f"'{provider}' does not support per-session API profiles")
    proto = profile["protocol"]
    if proto not in sup["protocols"]:
        raise ProfileError(f"{provider} {sup['note']}; this profile uses '{proto}'")
    key, base = profile.get("api_key", ""), profile["base_url"].rstrip("/")
    if not key:
        raise ProfileError("this profile has no API key saved")
    model = (model or profile.get("default_model") or "").strip()
    if not model:
        raise ProfileError("choose a model for this profile")
    if provider == "claude":
        env = {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_MODEL": model, "ANTHROPIC_SMALL_FAST_MODEL": model}
        return {"env": env, "args": [], "secrets": [key], "model": model}
    if provider == "codex":
        prov = '{ name = "K.O.D.E. profile", base_url = "%s", env_key = "KODE_API_KEY", wire_api = "responses" }' % base
        args = ["-c", 'model_provider="kode"', "-c", f"model_providers.kode={prov}", "-m", model]
        return {"env": {"KODE_API_KEY": key}, "args": args, "secrets": [key], "model": model}
    # opencode
    npm = "@ai-sdk/openai" if proto == "openai-responses" else "@ai-sdk/openai-compatible"
    cfg = {"$schema": "https://opencode.ai/config.json",
           "provider": {"kode": {"npm": npm, "name": profile.get("name", "K.O.D.E. profile"),
                                 "options": {"baseURL": base, "apiKey": "{env:KODE_API_KEY}"}, "models": {model: {"name": model}}}},
           "model": f"kode/{model}"}
    return {"env": {"OPENCODE_CONFIG_CONTENT": json.dumps(cfg, separators=(",", ":")), "KODE_API_KEY": key},
            "args": ["-m", f"kode/{model}"], "secrets": [key], "model": model}


# Extra launch flags the PROJECT engine may add so an agent can work unattended: flag -> (number of values, allowed values or None for free text).
PROJECT_FLAGS = {"--agent": (1, ("build",)), "--auto": (0, None), "--pure": (0, None), "--permission-mode": (1, ("acceptEdits",)),
                 "-s": (1, ("danger-full-access",)), "-a": (1, ("never",)), "--mode": (1, ("accept-edits",)),
                 "--dangerously-skip-permissions": (0, None)}


def project_args(provider: str, auto: bool, turbo: bool = False) -> List[str]:
    """Flags that put a provider into 'do the work' mode without editing its config.
    turbo = every tool permission pre-approved (fastest; the agent may run any command inside the project folder)."""
    if provider == "opencode":
        return ["--agent", "build"] + (["--auto"] if auto or turbo else [])
    if provider == "claude":
        return ["--dangerously-skip-permissions"] if turbo else ["--permission-mode", "acceptEdits"]
    if provider == "agy":                              # without these Antigravity stops on a review / permission screen for every step
        return ["--dangerously-skip-permissions"] if turbo else ["--mode", "accept-edits"]
    if provider == "codex" and (auto or turbo):         # Codex's Windows sandbox cannot start commands, and workspace-write blocks `git push` (no network)
        return ["-s", "danger-full-access", "-a", "never"]
    return []


def validate_launch(env: Dict[str, str], args: List[str]) -> Optional[str]:
    """Node-side check of what the hub asked it to inject. Returns an error string or None."""
    for k in env:
        if not ENV_ALLOW.match(k):
            return f"environment variable {k!r} is not allowed"
    i = 0
    while i < len(args):
        a = args[i]
        if a in ARG_FLAGS and i + 1 < len(args):
            i += 2
        elif a in PROJECT_FLAGS:
            n, allowed = PROJECT_FLAGS[a]
            if n and i + n >= len(args):
                return f"argument {a!r} needs a value"
            if n and allowed and args[i + 1] not in allowed:
                return f"value {args[i + 1]!r} is not allowed for {a}"
            i += 1 + n
        else:
            return f"argument {a!r} is not allowed"
    return None


async def test_profile(profile: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
    """Cheap reachability/auth check: list models. No tokens are spent. Falls back to saying exactly what was (not) verified."""
    import httpx                                     # only the Brain needs this; the node bundle stays light
    base, key, proto = profile["base_url"].rstrip("/"), profile.get("api_key", ""), profile["protocol"]
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Authorization": f"Bearer {key}"} if proto == "anthropic" else {"Authorization": f"Bearer {key}"}
    url = base + ("/v1/models" if proto == "anthropic" and not base.endswith("/v1") else "/models")
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            import time
            t0 = time.perf_counter()
            r = await c.get(url, headers=headers)
            ms = round((time.perf_counter() - t0) * 1000)
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"could not reach {url}: {type(e).__name__}", "ms": None}
    if r.status_code in (401, 403):
        return {"ok": False, "detail": f"the endpoint rejected the key (HTTP {r.status_code})", "ms": ms}
    if r.status_code >= 400:
        return {"ok": False, "detail": f"{url} answered HTTP {r.status_code}", "ms": ms}
    try:
        ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
    except Exception:
        ids = []
    return {"ok": True, "detail": f"reachable, key accepted, {len(ids)} models listed" if ids else "reachable, key accepted (no model list returned)",
            "ms": ms, "models": ids[:200]}
