"""LiteLLM wrapper. The ONLY place a model is called. No simulation path exists here:
a missing key raises MissingConfigError (see config.Settings.require_llm)."""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Optional, Protocol, Type, TypeVar

from pydantic import BaseModel, ValidationError

from config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMTransportError(RuntimeError):
    """Transient failures (connection/timeout/5xx/429) persisted past the retry budget."""


class LLMOutputError(RuntimeError):
    """The model answered, but not with usable/valid output (after one corrective retry)."""


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""


class LLMClient(Protocol):
    async def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> LLMResult: ...


class LiteLLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> LLMResult:
        key = self.settings.require_llm()          # raises loudly if missing
        import litellm                              # imported lazily: keeps startup fast

        resp = await litellm.acompletion(
            model=self.settings.llm_model,
            api_key=key,
            max_tokens=max_tokens,
            temperature=0.2,
            timeout=600,
            **({"api_base": self.settings.openai_api_base} if self.settings.openai_api_base else {}),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        usage = getattr(resp, "usage", None)
        return LLMResult(
            text=resp.choices[0].message.content or "",
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            finish_reason=resp.choices[0].finish_reason or "",
        )


def is_transient(exc: BaseException) -> bool:
    """Connection errors, timeouts, 429 and 5xx are retryable; auth/400/parse errors are not."""
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    name = type(exc).__name__
    if name in {"Timeout", "APIConnectionError", "InternalServerError", "ServiceUnavailableError",
                "RateLimitError", "BadGatewayError", "APITimeoutError", "ReadTimeout", "ConnectTimeout", "ConnectError"}:
        return True
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and (code >= 500 or code == 429):
        return True
    if name == "APIError":                         # litellm wraps gateway failures generically
        msg = str(exc).lower()
        return "connection error" in msg or "timed out" in msg or "bad gateway" in msg
    return False


class RetryingLLM:
    """Bounded retry with exponential backoff + jitter around any LLMClient. Fails loudly afterwards."""

    def __init__(self, inner: LLMClient, *, max_tries: int = 4, base_delay_s: float = 1.0,
                 deadline_s: float = 300.0, sleep=asyncio.sleep, on_retry=None):
        self.inner, self.max_tries, self.base_delay_s = inner, max_tries, base_delay_s
        self.deadline_s, self._sleep, self.on_retry = deadline_s, sleep, on_retry
        self.retries = 0                                  # observable counter (tests, banner)

    async def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> LLMResult:
        start = time.monotonic()
        last: Optional[BaseException] = None
        attempt = 0
        for attempt in range(1, self.max_tries + 1):
            remaining = self.deadline_s - (time.monotonic() - start)
            if remaining <= 0:
                break
            try:
                return await asyncio.wait_for(self.inner.complete(system, user, max_tokens=max_tokens), remaining)
            except asyncio.CancelledError:
                raise
            except Exception as e:                        # classified below
                if not is_transient(e):
                    raise                                  # auth/config/etc: never retried
                last = e
                if attempt == self.max_tries:
                    break
                delay = self.base_delay_s * (2 ** (attempt - 1)) * (0.75 + random.random() / 2)
                delay = min(delay, max(0.0, self.deadline_s - (time.monotonic() - start)))
                self.retries += 1
                if self.on_retry:
                    self.on_retry(attempt, e, delay)
                await self._sleep(delay)
        raise LLMTransportError(
            f"LLM unreachable after {attempt} attempt(s) / {time.monotonic() - start:.1f}s: "
            f"{type(last).__name__}: {str(last)[:200]}") from last


def build_llm(settings: Settings) -> "RetryingLLM":
    return RetryingLLM(LiteLLMClient(settings), max_tries=settings.llm_max_retries,
                       base_delay_s=settings.llm_base_delay_s, deadline_s=settings.llm_call_deadline_s)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def extract_json(text: str) -> dict:
    t = text.strip()
    m = _FENCE.match(t)
    if m:
        t = m.group(1)
    if not t.startswith("{"):                       # tolerate prose before/after the object
        a, b = t.find("{"), t.rfind("}")
        if a == -1 or b == -1:
            raise LLMOutputError("no JSON object in model output")
        t = t[a:b + 1]
    try:
        return json.loads(t, strict=False)                 # strict=False accepts raw newlines/tabs inside strings, a common model slip
    except json.JSONDecodeError as first:
        try:
            return json.loads(_repair_json(t), strict=False)
        except json.JSONDecodeError:
            raise LLMOutputError(f"invalid JSON from model: {first}") from first


def _repair_json(t: str) -> str:
    """Cheap local repair (no model call): escape raw control characters inside strings and drop trailing commas."""
    out, in_str, esc = [], False, False
    for ch in t:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == chr(92):
                out.append(ch)
                esc = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch == chr(10):
                out.append(chr(92) + "n")
            elif ch == chr(13):
                out.append(chr(92) + "r")
            elif ch == chr(9):
                out.append(chr(92) + "t")
            elif ord(ch) < 32:
                out.append(" ")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


async def complete_model(llm: LLMClient, system: str, user: str, model_cls: Type[T], *,
                         max_tokens: int = 8000, validator=None):
    """Call the LLM, parse + validate into `model_cls`; one corrective retry with the error.
    Returns (model, [LLMResult, ...]) so callers can report usage."""
    results = []
    feedback = ""
    last_err: Optional[Exception] = None
    budget = max_tokens
    for _ in range(3):
        res = await llm.complete(system, user + feedback, max_tokens=budget)
        results.append(res)
        if not res.text.strip():                     # empty completion (reasoning models can exhaust budget)
            last_err = LLMOutputError(f"empty completion [finish_reason={res.finish_reason!r}]")
            budget = int(budget * 1.5)
            continue
        if res.finish_reason == "length":            # output truncated at the token cap: retry with a bigger budget
            last_err = LLMOutputError(f"output truncated at {budget} tokens [chars={len(res.text)}]")
            budget = min(int(budget * 1.6), 64000)
            continue
        try:
            obj = model_cls.model_validate(extract_json(res.text))
            if validator:
                validator(obj)
            return obj, results
        except (LLMOutputError, ValidationError, ValueError) as e:
            last_err = LLMOutputError(f"{e} [finish_reason={res.finish_reason!r}, chars={len(res.text)}, "
                                      f"head={res.text[:120]!r}]")
            feedback = f"\n\nYour previous answer was rejected: {str(e)[:900]}\nReturn ONLY the corrected JSON object."
    raise LLMOutputError(f"model failed validation after 3 attempts: {last_err}")
