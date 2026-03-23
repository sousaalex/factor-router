"""
Factor AI — LLM Router
-----------------------
Escolhe o modelo mais adequado e mais barato para cada mensagem.
Baseado em Signal-Decision (vLLM SR 2025) e Router-R1 (UIUC NeurIPS 2025).
Nunca bloqueia o agente — fallback gracioso sempre.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import yaml
from dotenv import load_dotenv

from src.router.classifier_prompt import build_classifier_prompt

load_dotenv()
logger = logging.getLogger(__name__)


@dataclass
class RouterResult:
    model_id:                str
    input_tokens:            int
    output_tokens:           int
    raw_response:            str
    eval_duration_ms:        Optional[float] = None
    estimated_input_tokens:  int = 0
    estimated_output_tokens: int = 0

    def __str__(self) -> str:
        return self.model_id

    @property
    def estimated_total_tokens(self) -> int:
        return self.estimated_input_tokens + self.estimated_output_tokens


OLLAMA_BASE_URL    = os.getenv("OLLAMA_BASE_URL", "")
CLASSIFIER_MODEL   = os.getenv("CLASSIFIER_MODEL","")
CLASSIFIER_TIMEOUT = float(os.getenv("CLASSIFIER_TIMEOUT_SECONDS", "8.0"))

CONFIG_PATH = Path(__file__).parent / "models_config.yaml"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"models_config.yaml not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_CONFIG        = _load_config()
_MODELS        = _CONFIG["models"]
_DEFAULT_MODEL = _CONFIG["default_model"]
_VALID_IDS     = {m["id"] for m in _MODELS}

_BASE_CONTEXT_TOKENS = 4000
_BASE_OUTPUT_TOKENS  = 500
_CHARS_PER_TOKEN     = 3.5


def estimate_request_tokens(user_message: str) -> tuple[int, int]:
    if not user_message or not user_message.strip():
        return _BASE_CONTEXT_TOKENS, _BASE_OUTPUT_TOKENS
    msg_tokens = max(1, int(len(user_message.strip()) / _CHARS_PER_TOKEN))
    return _BASE_CONTEXT_TOKENS + msg_tokens, _BASE_OUTPUT_TOKENS + int(msg_tokens * 1.5)


def _parse_price(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    for prefix in ("$", "€", "£", "¥", " "):
        s = s.replace(prefix, "")
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return 0.0


def get_model_info(model_id: str) -> Optional[dict]:
    for m in _MODELS:
        if m.get("id") == model_id:
            pricing = m.get("pricing") or {}
            return {
                "id":                   model_id,
                "tier":                 m.get("tier"),
                "context_window":       m.get("context_window") or 0,
                "input_per_1m_tokens":  _parse_price(pricing.get("input_per_1m_tokens",  0)),
                "output_per_1m_tokens": _parse_price(pricing.get("output_per_1m_tokens", 0)),
            }
    return None


async def _call_classifier(user_message: str, est_in: int, est_out: int) -> tuple[str, int, int, Optional[float]]:
    system_prompt, user_prompt = build_classifier_prompt(
        user_message=user_message,
        models=_MODELS,
        default_model=_DEFAULT_MODEL,
        estimated_input_tokens=est_in,
        estimated_output_tokens=est_out,
    )
    payload = {
        "model":  CLASSIFIER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "stream": False,
        "think":  False,
        "options": {"temperature": 0.0, "num_predict": 64},
    }
    async with httpx.AsyncClient(timeout=CLASSIFIER_TIMEOUT) as client:
        response = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()

    content     = (data.get("message") or {}).get("content") or ""
    inp         = int(data.get("prompt_eval_count") or 0)
    out         = int(data.get("eval_count")        or 0)
    duration_ns = data.get("eval_duration")
    duration_ms = (float(duration_ns) / 1e6) if duration_ns is not None else None
    return content.strip(), inp, out, duration_ms


def _parse_model_from_response(raw: str) -> tuple[str, Optional[str]]:
    try:
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start != -1 and end > start:
            clean = clean[start:end]
        model_id = (json.loads(clean).get("model") or "").strip()
        if model_id in _VALID_IDS:
            return model_id, None
        logger.warning("[Router] Unknown model ID '%s' — fallback para: %s", model_id, _DEFAULT_MODEL)
        return _DEFAULT_MODEL, "unknown_model"
    except Exception as e:
        logger.warning("[Router] Failed to parse classifier response: '%s' — %s", raw[:100], e)
        return _DEFAULT_MODEL, "parse_error"


async def route(user_message: str) -> RouterResult:
    """
    Given the user message, returns the model to use.
    Always returns a valid result — never raises an exception.
    """
    if not user_message or not user_message.strip():
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0, raw_response="(empty message — default)")

    est_in, est_out = estimate_request_tokens(user_message)

    base = (OLLAMA_BASE_URL or "").strip().rstrip("/")
    if not base:
        logger.warning(
            "[Router] OLLAMA_BASE_URL não definido — a usar modelo default: %s",
            _DEFAULT_MODEL,
        )
        print(f"[Router] OLLAMA_BASE_URL não definido — falling back to: {_DEFAULT_MODEL}")
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(
            model_id=_DEFAULT_MODEL,
            input_tokens=0,
            output_tokens=0,
            raw_response="(OLLAMA_BASE_URL unset)",
            estimated_input_tokens=est_in,
            estimated_output_tokens=est_out,
        )

    try:
        content, inp, out, duration_ms = await _call_classifier(user_message, est_in, est_out)
        model_id, fallback = _parse_model_from_response(content)
        logger.info("[Router] '%s...' -> %s (est ~%d tokens, clf in=%d out=%d, %sms)",
                    user_message[:50], model_id, est_in + est_out, inp, out,
                    f"{duration_ms:.0f}" if duration_ms else "?")
        print(f"[LLMRouter] model: {model_id}")
        return RouterResult(model_id=model_id, input_tokens=inp, output_tokens=out,
                           raw_response=content, eval_duration_ms=duration_ms,
                           estimated_input_tokens=est_in, estimated_output_tokens=est_out)

    except httpx.TimeoutException:
        logger.warning("[Router] Classifier timeout (%.1fs) — falling back to: %s", CLASSIFIER_TIMEOUT, _DEFAULT_MODEL)
        print(f"[Router] Classifier timeout ({CLASSIFIER_TIMEOUT}s) — falling back to: {_DEFAULT_MODEL}")
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0,
                           raw_response="(timeout)", estimated_input_tokens=est_in, estimated_output_tokens=est_out)

    except httpx.ConnectError as e:
        hint = (
            " Dentro de Docker, usa OLLAMA_BASE_URL=http://host.docker.internal:11434 "
            "(ou o hostname do serviço na rede), não localhost."
        )
        logger.error(
            "[Router] Sem ligação ao Ollama em %s — %s.%s Falling back to: %s",
            OLLAMA_BASE_URL,
            e,
            hint,
            _DEFAULT_MODEL,
        )
        print(
            f"[Router] Sem ligação ao Ollama em {OLLAMA_BASE_URL!r}: {e}.{hint} "
            f"Falling back to: {_DEFAULT_MODEL}"
        )
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(
            model_id=_DEFAULT_MODEL,
            input_tokens=0,
            output_tokens=0,
            raw_response=f"(connect_error: {e})",
            estimated_input_tokens=est_in,
            estimated_output_tokens=est_out,
        )

    except httpx.RequestError as e:
        logger.error(
            "[Router] Erro de rede ao falar com Ollama (%s): %s — falling back to: %s",
            OLLAMA_BASE_URL,
            e,
            _DEFAULT_MODEL,
        )
        print(f"[Router] Erro de rede Ollama ({OLLAMA_BASE_URL}): {e} — falling back to: {_DEFAULT_MODEL}")
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(
            model_id=_DEFAULT_MODEL,
            input_tokens=0,
            output_tokens=0,
            raw_response=f"(request_error: {e})",
            estimated_input_tokens=est_in,
            estimated_output_tokens=est_out,
        )

    except Exception as e:
        logger.error("[Router] Unexpected error: %s — falling back to: %s", e, _DEFAULT_MODEL)
        print(f"[Router] Unexpected error: {e} — falling back to: {_DEFAULT_MODEL}")
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0,
                           raw_response=f"(error: {e})", estimated_input_tokens=est_in, estimated_output_tokens=est_out)