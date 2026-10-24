"""Minimal client for any OpenAI-compatible chat API (OpenRouter by default).

Settings come from the environment or a `.env` file in the working directory:
    OPENROUTER_API_KEY   required
    LLM_BASE_URL         default https://openrouter.ai/api/v1
"""

import json
import os
import random
import re
import time
from pathlib import Path

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


def load_env(path: Path = Path(".env")) -> None:
    """KEY=value lines into os.environ, without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip().removeprefix("export ").strip(), value.strip().strip("'\""))


class LLMError(RuntimeError):
    pass


class Client:
    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        load_env()
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is not set. Put it in a .env file in this folder:\n"
                             "  OPENROUTER_API_KEY=sk-or-...")
        self.model = model
        self.http = httpx.Client(
            base_url=base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
            headers={"Authorization": f"Bearer {key}", "X-Title": "hackathon-index"},
            timeout=120, transport=transport)

    def json(self, system: str, user: str, max_tokens: int = 600) -> tuple[dict, dict]:
        """One chat call that must answer with a JSON object. Returns (answer, usage);
        usage includes `cost` in USD when the provider reports it (OpenRouter does)."""
        body = {
            "model": self.model, "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
        }
        for attempt in range(6):
            try:
                resp = self.http.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                error = repr(exc)
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if "choices" in data:
                        text = data["choices"][0]["message"].get("content") or ""
                        return parse_json(text), data.get("usage") or {}
                    error = json.dumps(data.get("error", data))[:300]
                elif resp.status_code in RETRY_STATUSES:
                    error = f"HTTP {resp.status_code}"
                else:  # 400/401/402/404: retrying won't help
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            time.sleep(min(60, 2 ** attempt) + random.random())
        raise LLMError(f"gave up after retries: {error}")


def parse_json(text: str) -> dict:
    """The first JSON object in a reply, tolerating code fences or a preamble."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError(f"no JSON in reply: {text[:200]!r}")
        return json.loads(m.group())
