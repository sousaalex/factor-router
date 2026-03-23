"""
Normaliza message.content no formato OpenAI Chat Completions:
str, lista de partes multimodais (text / image_url), ou dict pontual.
"""
from __future__ import annotations

from typing import Any


def flatten_openai_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p.strip())
            elif isinstance(p, dict):
                if p.get("type") == "text":
                    t = p.get("text")
                    if isinstance(t, str):
                        parts.append(t.strip())
        return "\n".join(x for x in parts if x)
    if isinstance(content, dict) and content.get("type") == "text":
        t = content.get("text")
        return (t if isinstance(t, str) else "").strip()
    return str(content).strip()
