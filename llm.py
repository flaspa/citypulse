"""W&B Inference (OpenAI-compatible) helpers."""
import json
import re

from openai import AsyncOpenAI

import settings

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.WANDB_API_KEY:
            raise RuntimeError("WANDB_API_KEY is not set")
        _client = AsyncOpenAI(
            base_url=settings.WANDB_BASE_URL,
            api_key=settings.WANDB_API_KEY,
            default_headers={"OpenAI-Project": settings.WANDB_PROJECT_PATH},
        )
    return _client


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fenced:
        text = fenced.group(1)
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object in model output: {text[:200]!r}")


_json_mode_unsupported: set[str] = set()


async def chat_json(system: str, user: str, model: str = settings.DIRECTOR_MODEL,
                    temperature: float = 0.4, max_tokens: int = 8000) -> dict:
    last_err = None
    for _ in range(3):
        kw = {}
        if model not in _json_mode_unsupported:
            kw["response_format"] = {"type": "json_object"}
        try:
            r = await client().chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
                **kw,
            )
        except Exception as e:
            if kw and "response_format" in str(e):
                _json_mode_unsupported.add(model)
                last_err = e
                continue
            raise
        try:
            return parse_json(r.choices[0].message.content)
        except ValueError as e:
            last_err = e
    raise last_err
