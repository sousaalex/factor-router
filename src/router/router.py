"""
Factor AI — LLM Router
-----------------------
Escolhe o modelo mais adequado e mais barato para cada mensagem usando APENAS o classificador LLM (Ollama).
Sem heurística de keywords — o LLM decide baseado no prompt em classifier_prompt.py.

Timeout: CLASSIFIER_TIMEOUT_SECONDS (default: 2.0s)
Se o LLM demorar >2s → fallback para default_model do YAML.

Nunca bloqueia o agente — fallback gracioso sempre.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.gateway.openai_message_content import flatten_openai_message_content

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
CLASSIFIER_MODEL   = os.getenv("CLASSIFIER_MODEL", "qwen2.5:0.5b")
CLASSIFIER_TIMEOUT = float(os.getenv("CLASSIFIER_TIMEOUT_SECONDS", "2.0"))
# native → POST /api/chat (Ollama). openai → POST /v1/chat/completions (Ollama recente, LM Studio, etc.)
_CLASSIFIER_API_RAW = (os.getenv("OLLAMA_CLASSIFIER_API") or "native").strip().lower()

CONFIG_PATH = Path(__file__).parent / "models_config.yaml"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"models_config.yaml not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_CONFIG        = _load_config()
_MODELS        = _CONFIG["models"]
_DEFAULT_MODEL = _CONFIG["default_model"]
_VALID_IDS: set[str] = set()
for m in _MODELS:
    mid = m["id"]
    _VALID_IDS.add(mid)
    # Alias explícito openrouter/… (mesmo modelo OpenRouter que o id sem prefixo)
    if not mid.startswith("ollama/") and not mid.startswith("openrouter/"):
        _VALID_IDS.add(f"openrouter/{mid}")

# Modelo fixo do gateway (X-Conversation-Id: generate-title). Não está em models_config.yaml
# para não entrar no prompt do classificador; preços só em get_model_info abaixo.
GATEWAY_TITLE_MODEL_ID = "google/gemini-2.5-flash-lite"

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
    if model_id == GATEWAY_TITLE_MODEL_ID:
        return {
            "id":                   model_id,
            "tier":                 None,
            "context_window":       1_048_576,
            "input_per_1m_tokens":  0.10,
            "output_per_1m_tokens": 0.40,
        }
    lookup = model_id
    if model_id.startswith("openrouter/"):
        lookup = model_id[len("openrouter/") :]
    for m in _MODELS:
        mid = m.get("id")
        if mid == model_id or mid == lookup:
            pricing = m.get("pricing") or {}
            return {
                "id":                   model_id,
                "tier":                 m.get("tier"),
                "context_window":       m.get("context_window") or 0,
                "input_per_1m_tokens":  _parse_price(pricing.get("input_per_1m_tokens",  0)),
                "output_per_1m_tokens": _parse_price(pricing.get("output_per_1m_tokens", 0)),
            }
    return None


def _classifier_uses_openai_path() -> bool:
    return _CLASSIFIER_API_RAW in ("openai", "v1", "compatible", "openai_compatible")


def _normalize_match_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text)
    return text


def _content_has_image(content: Any) -> bool:
    if isinstance(content, list):
        for part in content:
            if _content_has_image(part):
                return True
        return False
    if isinstance(content, dict):
        part_type = str(content.get("type") or "").strip().lower()
        if part_type in {"image", "image_url", "input_image"}:
            return True
        data = content.get("image_url")
        if isinstance(data, dict) and data.get("url"):
            return True
        return False
    return False


def _looks_like_business_work(text: str) -> bool:
    tokens = (
        "invoice",
        "order",
        "customer",
        "client",
        "company",
        "account",
        "stock",
        "product",
        "quote",
        "budget",
        "erp",
        "many2one",
        "m2o",
        "purchase",
        "sale",
        "approval",
        "report",
        "revenue",
        "margin",
        "ledger",
    )
    return any(token in text for token in tokens)


def _heuristic_route_model(
    user_message: str,
    *,
    has_image: bool = False,
    tool_choice: Any = None,
    openrouter_balance_low: bool = False,
) -> str:
    """
    Fallback simples quando o LLM classifier falha ou timeout.
    Retorna o default_model configurado no YAML.
    """
    return _DEFAULT_MODEL


def _heuristic_is_confident(user_message: str, *, tool_choice: Any = None) -> bool:
    """
    Sempre retorna False para forçar o uso do LLM classifier.
    O router decide baseado apenas no LLM, sem heurística de keywords.
    """
    return False


async def _call_classifier(
    user_message: str,
    est_in: int,
    est_out: int,
    base: str,
    *,
    openrouter_balance_low: bool = False,
) -> tuple[str, int, int, Optional[float]]:
    system_prompt, user_prompt = build_classifier_prompt(
        user_message=user_message,
        models=_MODELS,
        default_model=_DEFAULT_MODEL,
        estimated_input_tokens=est_in,
        estimated_output_tokens=est_out,
        openrouter_balance_low=openrouter_balance_low,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    async with httpx.AsyncClient(timeout=CLASSIFIER_TIMEOUT) as client:
        if _classifier_uses_openai_path():
            url = f"{base}/v1/chat/completions"
            payload = {
                "model": CLASSIFIER_MODEL,
                "messages": messages,
                "stream": False,
                "temperature": 0,
                "max_tokens": 50,
            }
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            c0 = choices[0] if choices else {}
            msg = (c0.get("message") or {}) if isinstance(c0, dict) else {}
            content = (msg.get("content") or "") if isinstance(msg, dict) else ""
            usage = data.get("usage") or {}
            inp = int(usage.get("prompt_tokens") or 0)
            out = int(usage.get("completion_tokens") or 0)
            return content.strip(), inp, out, None

        url = f"{base}/api/chat"
        payload = {
            "model": CLASSIFIER_MODEL,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.0, "num_predict": 50},
        }
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    content = (data.get("message") or {}).get("content") or ""
    inp = int(data.get("prompt_eval_count") or 0)
    out = int(data.get("eval_count") or 0)
    duration_ns = data.get("eval_duration")
    duration_ms = (float(duration_ns) / 1e6) if duration_ns is not None else None
    return content.strip(), inp, out, duration_ms


def _parse_model_from_response(raw: str) -> tuple[str, Optional[str]]:
    """
    Parse classifier response.
    Accepts either {"model": "..."} or {"tier": N} format.
    Returns (model_id, fallback_reason).
    """
    try:
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start != -1 and end > start:
            clean = clean[start:end]
        data = json.loads(clean)

        # Try tier-based classification first
        tier = data.get("tier")
        if tier is not None:
            try:
                tier_int = int(tier)
                return f"__tier__:{tier_int}", None
            except (ValueError, TypeError):
                pass

        # Fallback to model_id based classification
        model_id = (data.get("model") or "").strip()
        if model_id in _VALID_IDS:
            return model_id, None
        logger.warning("[Router] Unknown model ID '%s' — fallback to: %s", model_id, _DEFAULT_MODEL)
        return _DEFAULT_MODEL, "unknown_model"
    except Exception as e:
        logger.warning("[Router] Failed to parse classifier response: '%s' — %s", raw[:100], e)
        return _DEFAULT_MODEL, "parse_error"


async def route(
    user_message: Any,
    *,
    openrouter_balance_low: bool = False,
    tool_choice: Any = None,
) -> RouterResult:
    """
    Router LLM puro — sem heurística, sem keywords, sem modo híbrido.
    
    Fluxo:
    1. Chama classificador LLM (Ollama) com timeout de 2s
    2. Se timeout/erro → retorna default_model do YAML
    3. Se tool_choice="required" → força openai/gpt-4.1-mini
    """
    user_message = flatten_openai_message_content(user_message)
    if not user_message or not user_message.strip():
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0, raw_response="(empty message)")

    est_in, est_out = estimate_request_tokens(user_message)
    base = (OLLAMA_BASE_URL or "").strip().rstrip("/")
    
    if not base:
        logger.warning("[Router] OLLAMA_BASE_URL não definido — fallback: %s", _DEFAULT_MODEL)
        print(f"[LLMRouter] model: {_DEFAULT_MODEL} (OLLAMA_BASE_URL unset)")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0, raw_response="(no ollama)")

    try:
        content, inp, out, duration_ms = await _call_classifier(
            user_message,
            est_in,
            est_out,
            base,
            openrouter_balance_low=openrouter_balance_low,
        )
        model_id, fallback_reason = _parse_model_from_response(content)
        
        if tool_choice == "required":
            model_id = "openai/gpt-4.1-mini"
        
        logger.info("[Router] '%s...' -> %s (%sms)", user_message[:50], model_id, f"{duration_ms:.0f}" if duration_ms else "?")
        print(f"[LLMRouter] model: {model_id}")
        return RouterResult(model_id=model_id, input_tokens=inp, output_tokens=out, raw_response=content, eval_duration_ms=duration_ms, estimated_input_tokens=est_in, estimated_output_tokens=est_out)

    except httpx.TimeoutException:
        logger.warning("[Router] Timeout (%.1fs) → fallback: %s", CLASSIFIER_TIMEOUT, _DEFAULT_MODEL)
        print(f"[LLMRouter] model: {_DEFAULT_MODEL} (timeout)")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0, raw_response="(timeout)")

    except Exception as e:
        logger.error("[Router] Erro → fallback: %s | %s", _DEFAULT_MODEL, e)
        print(f"[LLMRouter] model: {_DEFAULT_MODEL} (error: {e})")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0, raw_response=f"(error: {e})")
