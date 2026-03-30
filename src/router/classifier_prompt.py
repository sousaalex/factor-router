"""
Factor AI — Classifier Prompt
-------------------------------
Constrói os prompts para o classificador.
"""

# Número esperado de tool calls por tier — usado no prompt
TIER_TOOL_CALLS = {
    "simple":     "0-2",
    "vl-free":    "0-4",
    "reasoning":  "1-5",
    "reasoning+": "2-8",
    "complex":    "5-12",
    "frontier":   "10+",
}

# Contexto do agente em inglês — entra no prompt do classificador
AGENT_CONTEXT = """
AGENT CONTEXT:
  The Severino Agent operates on top of Agiweb.
  The agent has access to:
    - Agiweb tools: list_available_models, inspect_model_fields, execute_erp_command
    - Conversation history, user memory, permissions, installed modules, etc.
  The agent can execute up to 15 chained tool calls per request.
"""

# System prompt do classificador — inglês puro, raciocínio em 5 passos
CLASSIFIER_SYSTEM_PROMPT = """You are a model routing classifier for the Severino Agent.

Your ONLY job is to read the user message and decide which LLM model is best suited,
based on SEMANTIC PATTERNS in the message — not on keywords.

{agent_context}

HOW TO REASON (always follow these steps before deciding):

  STEP 1 — REASONING DEPTH:
    How many logical steps are needed to answer?
    Is the answer direct or does it require chaining logic?
    → Trivial / chat / no real ERP work = simple tier → qwen/qwen3.5-plus-02-15 (cheapest; 1M context)
    → Primarily video / multi-image documents / OCR or chart reading from media, minimal Agiweb, $0 only =
      vl-free tier → nvidia/nemotron-nano-12b-v2-vl:free (128K context; NOT for default ERP reliability)
    → 1 logical hop with real Agiweb or 1-5 linear tool calls = reasoning tier → qwen/qwen3.5-397b-a17b
    → 2-3 hops or mild multi-step = reasoning+ tier → moonshotai/kimi-k2.5 (NOT Claude)
    → 4+ hops with conditionals = complex (GPT-5.4 Mini) or true frontier (Claude only if justified)

  STEP 2 — TOOL CALLS:
    How many tools will the agent need to call?
    Is there a Many2one to resolve? (e.g. "for client BOLTHERM" requires an ID lookup)
    → 0-2 trivial calls or none = often simple tier (Plus); real ERP lookups → reasoning or higher
    → 1-5 linear calls = reasoning
    → 2-8 calls with some Many2one = reasoning+
    → 5-12 calls with conditional logic = complex
    → 10+ calls with expected self-correction = frontier

  STEP 3 — SYNTHESIS:
    Does the answer require combining data from multiple Agiweb models?
    Is there comparison across time periods, entities, or categories?
    → No Agiweb / pure language = simple tier when load is light
    → 1 Agiweb model = reasoning
    → 2-3 models with known relationships = reasoning+
    → 4+ models or unknown relationships = complex

  STEP 4 — ERROR COST:
    What is the impact if the agent makes a mistake?
    → Read/query operations = low risk → use cheaper model
    → Create/update critical records (orders, invoices, approvals, etc.) = high risk → use more capable model

  STEP 5 — DECISION:
    Pick the CHEAPEST model that can do the job CORRECTLY.
    Escalate only when necessary.

    PRODUCT VOCABULARY (do not confuse):
      - Tier "simple" = qwen/qwen3.5-plus-02-15 — light chat and minimal tool use only.
      - When the user says "reasoning+", "reasoning plus", or the product tier "reasoning+",
        they mean the Kimi K2.5 model (moonshotai/kimi-k2.5) — NOT Claude Sonnet.
      - Claude Sonnet is the FRONTIER tier: reserve it ONLY for extreme long-horizon agentic work,
        explicit requests for Claude / frontier / maximum capability, or complexity that clearly
        exceeds Kimi and GPT-5.4 Mini. It costs ~6.4x more on output than qwen/qwen3.5-397b-a17b — last resort.
      - Never map the phrase "reasoning+" to anthropic/claude-sonnet-4.6.

PRINCIPLE: Good, Clean, and Cheap.
  Underestimating complexity = agent fails, user loses trust.
  Overestimating complexity = unnecessary cost to the company.

RULES:
  - Reply with ONLY a valid JSON object. No explanation. No markdown. No extra text.
  - Format: {{"model": "provider/model-id"}}
  - Valid model IDs (use exactly one): {valid_model_ids}
  - When in doubt, use the default: {default_model}

AVAILABLE MODELS:
{models_description}
"""

LOW_OPENROUTER_BALANCE_BLOCK = """
---
OPENROUTER PREPAID BALANCE IS LOW (budget mode - act now):
  The organization's OpenRouter credit remaining is at or below the configured threshold.
  Minimize cost while still answering correctly:
  - Prefer qwen/qwen3.5-plus-02-15 for truly simple / chat-only turns; else qwen/qwen3.5-397b-a17b for light ERP.
  - Use moonshotai/kimi-k2.5 only when Many2one resolution or multi-step synthesis clearly needs reasoning+.
  - Do NOT pick openai/gpt-5.4-mini unless incorrect output would cause serious business harm
    AND Qwen 397B (reasoning) / Kimi are clearly insufficient for the workflow.
  - Do NOT pick anthropic/claude-sonnet-4.6 unless the user explicitly asks for Claude, Sonnet,
    or "frontier" / maximum capability by name.
---
"""

# User prompt — inglês, inclui estimativa de tokens
CLASSIFIER_USER_PROMPT = """User message: "{user_message}"

Estimated tokens: input ~{est_input}, output ~{est_output}, total ~{est_total}.
Consider each model's context window and cost when deciding.

Reply with ONLY valid JSON: {{"model": "provider/model-id"}}"""


def _format_context_window(n) -> str:
    """Formata o context window para display no prompt."""
    if n is None:
        return "?"
    if isinstance(n, int):
        if n >= 1_000_000:
            return f"{n // 1_000_000}M tokens"
        if n >= 1_000:
            return f"{n // 1_000}K tokens"
        return str(n)
    return str(n)


def build_models_description(models: list[dict]) -> str:
    """
    Constrói a descrição completa de cada modelo para o classificador.
    Toda a descrição está em inglês — entra directamente no prompt da rede neural.
    """
    lines = []
    for m in models:
        tier        = m.get("tier", "?")
        pricing     = m.get("pricing") or {}
        input_cost  = pricing.get("input_per_1m_tokens",  "?")
        output_cost = pricing.get("output_per_1m_tokens", "?")
        ctx_str     = _format_context_window(m.get("context_window"))
        tool_calls  = TIER_TOOL_CALLS.get(tier, "?")

        lines.append("---")
        lines.append(f"MODEL: {m['id']}")
        lines.append(
            f"TIER: {tier} | EXPECTED TOOL CALLS: {tool_calls} | "
            f"CONTEXT: {ctx_str} | COST: {input_cost} input / {output_cost} output per 1M tokens"
        )
        lines.append("")
        lines.append(m.get("description", "").strip())
        lines.append("")

        best_for = m.get("best_for") or []
        if best_for:
            lines.append("BEST FOR:")
            for use_case in best_for:
                lines.append(f"  - {use_case.strip()}")

        not_for = (m.get("not_for") or "").strip()
        if not_for:
            lines.append(f"DO NOT USE WHEN: {not_for}")

        lines.append("")

    return "\n".join(lines)


def _valid_model_ids(models: list[dict]) -> str:
    """Devolve os IDs válidos formatados para o prompt."""
    return ", ".join(f'"{m["id"]}"' for m in models)


def build_classifier_prompt(
    user_message: str,
    models: list[dict],
    default_model: str,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
    *,
    openrouter_balance_low: bool = False,
) -> tuple[str, str]:
    """
    Constrói o system prompt e o user prompt para o classificador Ollama.

    Tudo o que entra na rede neural está em inglês.
    O classificador raciocina em 5 passos antes de decidir o modelo.

    Returns:
        (system_prompt, user_prompt)
    """
    models_desc = build_models_description(models)
    valid_ids   = _valid_model_ids(models)
    est_total   = estimated_input_tokens + estimated_output_tokens

    system = CLASSIFIER_SYSTEM_PROMPT.format(
        agent_context=AGENT_CONTEXT.strip(),
        valid_model_ids=valid_ids,
        default_model=default_model,
        models_description=models_desc,
    )
    if openrouter_balance_low:
        system = system + "\n" + LOW_OPENROUTER_BALANCE_BLOCK.strip() + "\n"

    user = CLASSIFIER_USER_PROMPT.format(
        user_message=user_message,
        est_input=estimated_input_tokens,
        est_output=estimated_output_tokens,
        est_total=est_total,
    )

    return system, user