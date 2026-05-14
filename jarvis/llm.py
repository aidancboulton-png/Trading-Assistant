"""Anthropic Claude wrapper. Graceful fallback when no key is configured."""
from __future__ import annotations
import json, os, logging
from typing import Any

log = logging.getLogger(__name__)


class LLM:
    def __init__(self, api_key: str | None, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key
        self.model = model
        self.client = None
        if api_key and not api_key.startswith("sk-ant-...") and len(api_key) > 20:
            try:
                import anthropic  # type: ignore
                self.client = anthropic.Anthropic(api_key=api_key)
            except Exception as e:
                log.warning("Anthropic client init failed: %s", e)

    def available(self) -> bool:
        return self.client is not None

    def complete(self, prompt: str, max_tokens: int = 800,
                 system: str | None = None) -> str:
        if not self.client:
            return "[LLM unavailable — set anthropic_api_key in config.json]"
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system
            msg = self.client.messages.create(**kwargs)
            return msg.content[0].text.strip()
        except Exception as e:
            log.error("LLM call failed: %s", e)
            return f"[LLM error: {e}]"

    def json_complete(self, prompt: str, schema_hint: str = "",
                      max_tokens: int = 800) -> dict:
        """Ask for JSON; parse leniently."""
        sys = (
            "You are a precise analytical assistant. "
            "Always respond with a single valid JSON object — no prose, no code fences. "
            f"Schema hint: {schema_hint}" if schema_hint else
            "You are a precise analytical assistant. Always respond with a single valid JSON object."
        )
        text = self.complete(prompt, max_tokens=max_tokens, system=sys)
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        # Trim trailing junk after final }
        last = text.rfind("}")
        if last != -1:
            text = text[: last + 1]
        try:
            return json.loads(text)
        except Exception:
            log.warning("LLM JSON parse failed; returning raw under 'text' key")
            return {"text": text}
