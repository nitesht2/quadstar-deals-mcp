"""
llm.py - LLM Interface (DeepSeek Flash primary, OpenRouter fallback)

Provider-agnostic text + structured generation over the OpenAI-compatible API.
Tries DeepSeek Flash first (cheap, automatic context caching). On any error or
rate-limit, falls back to OpenRouter's free tier. No LangChain, no local model.

Env:
    DEEPSEEK_API_KEY   - primary provider key (or LLM_API_KEY)
    LLM_MODEL          - primary model (default deepseek-v4-flash)
    OPENROUTER_API_KEY - secondary provider key (optional)
    OPENROUTER_MODEL   - secondary model (default a free OpenRouter model)
"""

import json
import os

# --- Configuration ---
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
LLM_PROVIDER = "deepseek"  # primary; kept for backwards-compat imports


def _resolve_api_key() -> str | None:
    return os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY")


# Module-level alias for backwards compat
LLM_API_KEY = _resolve_api_key()


def _providers():
    """Yield (name, api_key, base_url, model, extra_body) in fallback order.

    DeepSeek first, then OpenRouter if its key is set. Providers with no key
    are skipped, so a missing OpenRouter key simply means DeepSeek-only.
    """
    deepseek_key = _resolve_api_key()
    if deepseek_key:
        yield (
            "deepseek",
            deepseek_key,
            "https://api.deepseek.com",
            LLM_MODEL,
            {"thinking": {"type": "disabled"}},
        )
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key:
        yield (
            "openrouter",
            openrouter_key,
            "https://openrouter.ai/api/v1",
            OPENROUTER_MODEL,
            {},  # OpenRouter ignores the deepseek 'thinking' param
        )


def _client(api_key: str, base_url: str):
    import openai
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def generate(prompt: str, max_tokens: int = 400, system: str | None = None) -> str | None:
    """Generate text. Tries DeepSeek, then OpenRouter. Returns None if all fail."""
    providers = list(_providers())
    if not providers:
        print("    No LLM key set. Set DEEPSEEK_API_KEY (or OPENROUTER_API_KEY) in .env.")
        return None

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    for name, api_key, base_url, model, extra_body in providers:
        try:
            client = _client(api_key, base_url)
            print(f"    LLM: {name} ({model}), key=set ({api_key[:8]}...)")
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                temperature=0.7,
                extra_body=extra_body or None,
            )
            text = response.choices[0].message.content.strip()
            print(f"    {name} returned {len(text)} chars")
            return text
        except Exception as e:
            print(f"    {name} API error: {e}")
            continue  # fall through to next provider

    return None


def generate_structured(prompt: str, schema, max_tokens: int = 1200):
    """Generate a structured Pydantic object via JSON mode.

    Tries each provider's native response_format=json_object. Hydrates the
    given schema (Pydantic model or plain dict). Returns (instance, confidence),
    or (None, 0.0) if every provider fails.
    """
    providers = list(_providers())
    if not providers:
        return None, 0.0

    schema_doc = json.dumps(schema.schema() if hasattr(schema, "schema") else {"type": "object"})
    json_prompt = f"""{prompt}

IMPORTANT: Respond ONLY with valid JSON matching this schema:
{schema_doc}

No markdown, no explanation, no code fences. Just the JSON object."""

    for name, api_key, base_url, model, extra_body in providers:
        try:
            client = _client(api_key, base_url)
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": json_prompt}],
                temperature=0,
                extra_body=extra_body or None,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content.strip())
            instance = schema(**data) if hasattr(schema, "__call__") else data
            return instance, 1.0
        except Exception as e:
            print(f"    generate_structured failed ({name}): {e}")
            continue

    # Last resort: plain generate() + json.loads (also provider-fallback aware)
    try:
        text = generate(prompt, max_tokens, system="Respond in JSON only. No markdown.")
        if text:
            data = json.loads(text)
            instance = schema(**data) if hasattr(schema, "__call__") else data
            return instance, 0.9
    except Exception:
        pass
    return None, 0.0
