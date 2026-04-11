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
CLASSIFIER_TIMEOUT = float(os.getenv("CLASSIFIER_TIMEOUT_SECONDS", "2.5"))
ROUTER_DECISION_MODE = os.getenv("ROUTER_DECISION_MODE", "heuristic").strip().lower()
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
    openrouter_balance_low: bool = False,
) -> str:
    """
    Decide o modelo localmente, sem chamar um LLM.

    O objectivo é devolver o `model_id` em poucas microssegundos/milissegundos
    e reservar o classificador LLM para um modo explícito de compatibilidade.
    """
    text = _normalize_match_text(user_message)
    if not text:
        return _DEFAULT_MODEL

    if any(term in text for term in ("gpt-5.4-mini", "gpt 5.4 mini", "complex tier", "complex")):
        return "openai/gpt-5.4-mini"

    if any(term in text for term in ("frontier", "maximum capability", "maximum", "best available")):
        return "openai/gpt-5.4-mini"

    if any(term in text for term in ("reasoning+", "reasoning plus", "kimi", "k2.5", "kimi k2.5")):
        return "moonshotai/kimi-k2.5"

    if has_image or any(term in text for term in ("screenshot", "image", "chart", "plot", "diagram", "mockup", "pdf", "vision", "visual")):
        if any(term in text for term in ("code", "coding", "refactor", "debug", "implement", "ui", "frontend")):
            return "moonshotai/kimi-k2.5"
        if len(text) > 8_000 or any(term in text for term in ("document", "transcript", "logs", "paste", "long context")):
            return "qwen/qwen3.5-plus-02-15"
        return "qwen/qwen3.5-plus-02-15"

    if len(text) > 12_000 or any(term in text for term in ("long context", "full repo", "full file", "entire repo", "transcript", "log dump")):
        return "qwen/qwen3.5-plus-02-15"

    if any(term in text for term in ("many2one", "lookup", "resolve id", "resolve the id", "multi-step", "multi step", "conditional", "if then", "if/else", "cascade")):
        return "moonshotai/kimi-k2.5"

    if any(term in text for term in ("create", "update", "delete", "approve", "assign", "sync", "orchestrate")) and _looks_like_business_work(text):
        return "qwen/qwen3.5-397b-a17b"

    if any(term in text for term in ("code", "bug", "fix", "refactor", "implement", "test", "repo", "pull request", "python", "typescript", "javascript", "react", "api", "endpoint", "class", "function")):
        return "qwen/qwen3.5-397b-a17b"

    if _looks_like_business_work(text):
        return "qwen/qwen3.5-397b-a17b"

    return "qwen/qwen3.5-397b-a17b"


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
                "max_tokens": 16,
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
            "options": {"temperature": 0.0, "num_predict": 16},
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


async def route(user_message: Any, *, openrouter_balance_low: bool = False) -> RouterResult:
    """
    Given the user message, returns the model to use.
    Accepta str ou content OpenAI multimodal (lista de partes).
    Always returns a valid result — never raises an exception.
    """
    raw_user_message = user_message
    user_message = flatten_openai_message_content(user_message)
    if not user_message or not user_message.strip():
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0, raw_response="(empty message — default)")

    est_in, est_out = estimate_request_tokens(user_message)

    if ROUTER_DECISION_MODE != "llm":
        model_id = _heuristic_route_model(
            user_message,
            has_image=_content_has_image(raw_user_message),
            openrouter_balance_low=openrouter_balance_low,
        )
        logger.info(
            "[Router] heuristic '%s...' -> %s (mode=%s)",
            user_message[:50],
            model_id,
            ROUTER_DECISION_MODE,
        )
        print(f"[LLMRouter] model: {model_id}")
        return RouterResult(
            model_id=model_id,
            input_tokens=0,
            output_tokens=0,
            raw_response=f"(heuristic:{ROUTER_DECISION_MODE})",
            eval_duration_ms=0.0,
            estimated_input_tokens=est_in,
            estimated_output_tokens=est_out,
        )

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
        content, inp, out, duration_ms = await _call_classifier(
            user_message,
            est_in,
            est_out,
            base,
            openrouter_balance_low=openrouter_balance_low,
        )
        model_id, _fallback = _parse_model_from_response(content)
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

    except httpx.HTTPStatusError as e:
        url = str(e.request.url)
        code = e.response.status_code
        openai_hint = (
            " Define no .env: OLLAMA_CLASSIFIER_API=openai "
            "(usa POST /v1/chat/completions — Ollama com API OpenAI, LM Studio, etc.)."
        )
        native_hint = (
            " Confirma que é Ollama com endpoint /api/chat ou usa OLLAMA_CLASSIFIER_API=native."
        )
        hint = openai_hint if code == 404 and "/api/chat" in url else native_hint if code == 404 else ""
        logger.error(
            "[Router] HTTP %s ao classificar (%s): %s.%s Falling back to: %s",
            code,
            url,
            e.response.reason_phrase or "",
            hint,
            _DEFAULT_MODEL,
        )
        print(
            f"[Router] HTTP {code} no classificador ({url}).{hint} "
            f"Falling back to: {_DEFAULT_MODEL}"
        )
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(
            model_id=_DEFAULT_MODEL,
            input_tokens=0,
            output_tokens=0,
            raw_response=f"(http_{code})",
            estimated_input_tokens=est_in,
            estimated_output_tokens=est_out,
        )

    except Exception as e:
        logger.error("[Router] Unexpected error: %s — falling back to: %s", e, _DEFAULT_MODEL)
        print(f"[Router] Unexpected error: {e} — falling back to: {_DEFAULT_MODEL}")
        print(f"[LLMRouter] model: {_DEFAULT_MODEL}")
        return RouterResult(model_id=_DEFAULT_MODEL, input_tokens=0, output_tokens=0,
                           raw_response=f"(error: {e})", estimated_input_tokens=est_in, estimated_output_tokens=est_out)
