import json
import logging
import re
from typing import Literal

import openai

from src.utils.settings import settings

logger = logging.getLogger(__name__)

Tier = Literal["small", "large"]

# MiniMax models may put their reasoning in the reply as <think>...</think>
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    """Any AI failure (no key, network, timeout, rate limit, bad output). Callers turn it into a 503."""


_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    # created on first use, so the app can start (and non-AI routes work) without a key
    global _client
    if _client is None:
        if settings.MINIMAX_API_KEY is None or not settings.MINIMAX_API_KEY.get_secret_value():
            raise LLMError("MINIMAX_API_KEY is not set")
        # MiniMax speaks the OpenAI API, so the OpenAI SDK works with a different base_url
        _client = openai.OpenAI(
            base_url=settings.MINIMAX_BASE_URL,
            api_key=settings.MINIMAX_API_KEY.get_secret_value(),
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=1,  # one quick retry on 429/5xx/network errors; then we try the fallback model
        )
    return _client


def _model_for(tier: Tier) -> str:
    return settings.LLM_MODEL_SMALL if tier == "small" else settings.LLM_MODEL_LARGE


def _extract_json(text: str) -> dict:
    # drop any <think>...</think> reasoning first: it can contain "{" and "}" too
    text = THINK_RE.sub("", text)
    # models sometimes wrap JSON in ```json fences or add a sentence around it,
    # so take everything from the first "{" to the last "}"
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("AI reply contains no JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError("AI returned invalid JSON") from e
    if not isinstance(data, dict):
        raise LLMError("AI returned JSON that is not an object")
    return data


def _check_value(name: str, value, rules: dict):
    """Checks one value against its schema rules and returns it (7.0 becomes 7)."""
    if rules["type"] == "string":
        if not isinstance(value, str):
            raise LLMError(f"AI reply: '{name}' should be a string")
        if "enum" in rules and value not in rules["enum"]:
            raise LLMError(f"AI reply: '{name}' must be one of {rules['enum']}")
    elif rules["type"] == "integer":
        if isinstance(value, float) and value.is_integer():
            value = int(value)  # some models write 7.0 instead of 7
        # bool is a subclass of int in Python, so True would pass as 1 without this check
        if not isinstance(value, int) or isinstance(value, bool):
            raise LLMError(f"AI reply: '{name}' should be a whole number")
        if not rules.get("minimum", value) <= value <= rules.get("maximum", value):
            raise LLMError(f"AI reply: '{name}' is out of range")
    elif rules["type"] == "array":
        if not isinstance(value, list):
            raise LLMError(f"AI reply: '{name}' should be a list")
        value = [_check_value(f"{name}[{i}]", item, rules["items"]) for i, item in enumerate(value)]
    return value


def _check_schema(data: dict, schema: dict) -> None:
    """Small check for the simple schemas we use: required keys, strings, enums, integers with
    minimum/maximum, and lists of those.
    Models don't always follow the requested format, so we verify it ourselves."""
    for key in schema.get("required", []):
        if key not in data:
            raise LLMError(f"AI reply is missing '{key}'")
    for key, rules in schema["properties"].items():
        if key in data:
            data[key] = _check_value(key, data[key], rules)


def generate_json(
    system: str, prompt: str, schema: dict, tier: Tier = "small", max_tokens: int = 16000
) -> dict:
    """Asks the model for JSON matching `schema` and returns it as a dict.

    Raises LLMError on any failure. Error messages never include the API key.
    """
    client = _get_client()
    format_rules = (
        "\n\nReply with only one JSON object, no other text and no markdown. "
        f"It must match this JSON Schema:\n{json.dumps(schema)}"
    )
    messages = [
        {"role": "system", "content": system + format_rules},
        {"role": "user", "content": prompt},
    ]
    # main model first; if it's overloaded or rate limited, try the fallback model once
    models = [_model_for(tier)]
    if settings.LLM_MODEL_FALLBACK and settings.LLM_MODEL_FALLBACK not in models:
        models.append(settings.LLM_MODEL_FALLBACK)

    for model in models:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                # thinking tokens count towards this limit, so keep it generous
                max_completion_tokens=max_tokens,
                # MiniMax option: put the model's thinking in a separate field, not in the reply text
                extra_body={"reasoning_split": True},
            )
            break
        except openai.APIStatusError as e:  # 4xx/5xx, e.g. 429 rate limit or 503 overloaded
            logger.warning("LLM call failed: HTTP %s from model %s", e.status_code, model)
            if (e.status_code == 429 or e.status_code >= 500) and model != models[-1]:
                continue  # try the next model
            raise LLMError(f"AI provider returned HTTP {e.status_code}") from e
        except openai.APIConnectionError as e:  # also covers timeouts
            logger.warning("LLM call failed: %s", type(e).__name__)
            raise LLMError("Could not reach the AI provider") from e

    if not response.choices:
        raise LLMError("AI returned no answer")
    choice = response.choices[0]
    if choice.finish_reason not in ("stop", None):
        # "length" = JSON was cut off, "content_filter" = blocked by the provider
        raise LLMError(f"AI response incomplete ({choice.finish_reason})")

    data = _extract_json(choice.message.content or "")
    _check_schema(data, schema)
    return data
