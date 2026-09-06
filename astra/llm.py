"""LLM client. OpenAI-compatible chat over urllib (no third-party deps).

Providers are selected by env:
  ASTRA_LLM            openai | mock   (default: openai if ASTRA_LLM_API_KEY or base url set, else mock)
  ASTRA_LLM_BASE_URL   e.g. https://api.openai.com/v1, http://localhost:1234/v1 (LM Studio), TensorMux, OpenRouter
  ASTRA_LLM_API_KEY
  ASTRA_MODEL_FAST     cheap/fast model used for retrieval-heavy, skill-known runs and reflection
  ASTRA_MODEL_STRONG   larger model used only for novel tasks

Every call returns usage + estimated USD so the controller can budget cost honestly.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# USD per 1M tokens (input, output). Unknown models fall back to DEFAULT_PRICE.
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-sonnet-4": (3.00, 15.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "llama-3.1-8b": (0.05, 0.08),
    "llama-3.3-70b": (0.59, 0.79),
    "mock-fast": (0.10, 0.40),
    "mock-strong": (2.00, 8.00),
}
DEFAULT_PRICE = (1.00, 4.00)


def _load_dotenv() -> None:
    p = Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def price_for(model: str) -> tuple[float, float]:
    for key, p in PRICES.items():
        if key in model:
            return p
    return DEFAULT_PRICE


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    cost_usd: float
    raw: dict = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class LLM:
    """OpenAI-compatible chat client."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: int = 120):
        self.base_url = (base_url or os.environ.get("ASTRA_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("ASTRA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "lm-studio"
        self.timeout = timeout

    def chat(self, model: str, messages: list[dict], temperature: float = 0.2, json_mode: bool = True,
             max_tokens: int = 4000) -> LLMResult:
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        effort = os.environ.get("ASTRA_REASONING_EFFORT")
        if effort:
            body["reasoning_effort"] = effort  # honoured by Gemini/OpenAI reasoning models, ignored elsewhere
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            if json_mode and "response_format" in detail:
                # Some OpenAI-compatible servers reject response_format; retry plain.
                return self.chat(model, messages, temperature, json_mode=False, max_tokens=max_tokens)
            raise RuntimeError(f"LLM HTTP {e.code}: {detail}") from e
        latency = time.perf_counter() - t0
        choice = payload["choices"][0]
        text = choice["message"].get("content") or ""
        usage = payload.get("usage") or {}
        if choice.get("finish_reason") == "length" and max_tokens < 24000:
            # Reasoning models spend hidden thinking tokens inside max_tokens; a truncated JSON reply is useless.
            # Retry once with a much larger cap and account for both attempts.
            retry = self.chat(model, messages, temperature, json_mode, max_tokens=max_tokens * 3)
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("total_tokens") or 0) - pt
            pin, pout = price_for(model)
            retry.cost_usd += (pt * pin + max(ct, 0) * pout) / 1_000_000
            retry.latency_s += latency
            retry.prompt_tokens += pt
            retry.completion_tokens += max(ct, 0)
            return retry
        pt = int(usage.get("prompt_tokens") or sum(estimate_tokens(m.get("content", "")) for m in messages))
        ct = int(usage.get("completion_tokens") or estimate_tokens(text))
        if usage.get("total_tokens"):
            ct = max(ct, int(usage["total_tokens"]) - pt)  # include billed reasoning/thinking tokens
        pin, pout = price_for(model)
        cost = (pt * pin + ct * pout) / 1_000_000
        return LLMResult(text=text, model=model, prompt_tokens=pt, completion_tokens=ct, latency_s=latency,
                         cost_usd=cost, raw=payload)


class MockLLM:
    """Offline stand-in. Delegates to a scripted policy supplied by the caller.

    Used for smoke tests and for running the loop without API keys. The mock still
    consumes memory the same way a real model would (via the prompt the actor builds),
    so memory growth changes behaviour deterministically. It is not the demo model.
    """

    def __init__(self, policy):
        self.policy = policy  # callable(model, messages) -> str

    def chat(self, model: str, messages: list[dict], temperature: float = 0.0, json_mode: bool = True,
             max_tokens: int = 1200) -> LLMResult:
        t0 = time.perf_counter()
        text = self.policy(model, messages)
        # Simulate strong model being slower.
        time.sleep(0.03 if "strong" in model else 0.01)
        latency = time.perf_counter() - t0
        pt = sum(estimate_tokens(m.get("content", "")) for m in messages)
        ct = estimate_tokens(text)
        pin, pout = price_for(model)
        return LLMResult(text=text, model=model, prompt_tokens=pt, completion_tokens=ct, latency_s=latency,
                         cost_usd=(pt * pin + ct * pout) / 1_000_000)


def parse_json(text: str) -> dict:
    """Tolerant JSON extraction from a model reply."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"thought": text[:400], "final": None, "parse_error": True}


def make_llm():
    mode = os.environ.get("ASTRA_LLM")
    if mode is None:
        mode = "openai" if (os.environ.get("ASTRA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
                            or os.environ.get("ASTRA_LLM_BASE_URL")) else "mock"
    if mode == "mock":
        from astra.mock_policy import scripted_policy
        return MockLLM(scripted_policy), {"fast": "mock-fast", "strong": "mock-strong"}
    if mode == "gemini":
        key = os.environ.get("ASTRA_LLM_API_KEY") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise SystemExit("ASTRA_LLM=gemini but no GEMINI_API_KEY / ASTRA_LLM_API_KEY set (put it in .env)")
        os.environ.setdefault("ASTRA_REASONING_EFFORT", "low")
        return LLM(base_url=os.environ.get("ASTRA_LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
                   api_key=key), {
            "fast": os.environ.get("ASTRA_MODEL_FAST", "gemini-2.5-flash"),
            "strong": os.environ.get("ASTRA_MODEL_STRONG", "gemini-2.5-pro"),
        }
    models = {
        "fast": os.environ.get("ASTRA_MODEL_FAST", "gpt-4o-mini"),
        "strong": os.environ.get("ASTRA_MODEL_STRONG", "gpt-4o"),
    }
    return LLM(), models
