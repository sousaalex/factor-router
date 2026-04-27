"""
Factor AI — Classifier Prompt
-------------------------------
Builds system and user prompts for the routing classifier.

O LLM é "burro" — precisa de instruções claras sobre:
1. Quais modelos existem
2. Quando usar cada um
3. Preços (para decidir o mais barato possível)
"""

# Agent context (English) — injected into the classifier prompt
AGENT_CONTEXT = """
AGENT CONTEXT:
  The Severino Agent operates on top of Agiweb.
  The agent has access to:
    - Agiweb tools: list_available_models, inspect_model_fields, execute_erp_command
    - Conversation history, user memory, permissions, installed modules, etc.
  The agent can execute up to 15 chained tool calls per request.
"""

# Classifier system prompt — ensina o LLM sobre CRITÉRIOS (sem nomes de modelos)
CLASSIFIER_SYSTEM_PROMPT = """You are a model routing classifier for the Severino Agent.

YOUR JOB:
Given a user message, choose the BEST model by returning its model_id.
Prefer LOCAL models when possible — they cost $0.
Only use CLOUD models when the task requires capabilities not available locally.

MODEL CATEGORIES:

LOCAL MODELS (factorai/*) — COST $0:
  • Small local model (0.5B - 1B params)
    - Use for: simple greetings, thanks, basic classification
    - Examples: "olá", "bom dia", "boa tarde", "obrigado", "thanks"
    
  • Large local model (35B+ params, MoE)
    - Use for: ALMOST EVERYTHING — this is your DEFAULT
    - Code generation, debugging, refactoring
    - Business logic, ERP queries, data analysis
    - Reasoning tasks, multi-step operations
    - Document analysis, summarization
    - Tool calls (1-5 tools)

CLOUD MODELS (openrouter/*) — COST MONEY:
  • Use ONLY when:
    - Task requires 256K+ context (larger than local models)
    - Specialized vision analysis (charts, complex diagrams)
    - Many2one resolution across multiple entities
    - 5+ tool calls in sequence
    - Task explicitly requires a specific cloud model

DECISION RULES:
1. ALWAYS prefer local models (factorai/*) — cost $0
2. For greetings (olá, bom dia, obrigado) → small local model
3. For EVERYTHING ELSE → large local model (your default choice)
4. Only use cloud models when task EXPLICITLY requires capabilities not in local models

RESPONSE FORMAT:
Reply with ONLY: {{"model": "model_id"}}
Example: {{"model": "moonshotai/kimi-k2.6"}}
NO explanation, NO markdown, ONLY the JSON.
"""

LOW_BUDGET_BLOCK = """
---
BUDGET MODE: You MUST use factorai/* models ONLY. Never use openrouter/* models.
---
"""

# User prompt — simple instruction
CLASSIFIER_USER_PROMPT = """User message: "{user_message}"

Choose the best model from the list above.
Reply with ONLY: {{"model": "model_id"}}"""


def _render_models_catalog(models: list[dict]) -> str:
    """Render a strict allow-list of model IDs for the classifier."""
    lines: list[str] = []
    for model in models:
        model_id = str(model.get("id") or "").strip()
        if not model_id:
            continue
        tier = str(model.get("tier") or "unknown").strip()
        lines.append(f"- {model_id} (tier: {tier})")

    catalog = "\n".join(lines) if lines else "- (no models configured)"
    return (
        "ALLOWED MODEL IDS (STRICT):\n"
        f"{catalog}\n\n"
        "IMPORTANT:\n"
        "- You MUST return one model_id EXACTLY from ALLOWED MODEL IDS.\n"
        "- Never invent model names.\n"
        "- If uncertain, choose the safest general-purpose model from ALLOWED MODEL IDS.\n"
    )


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
    Build (system_prompt, user_prompt) for the routing classifier.

    All neural-facing text is English.
    """
    catalog_block = _render_models_catalog(models)
    system = CLASSIFIER_SYSTEM_PROMPT + "\n\n" + catalog_block
    if openrouter_balance_low:
        system = system + "\n" + LOW_BUDGET_BLOCK.strip() + "\n"

    user = CLASSIFIER_USER_PROMPT.format(
        user_message=user_message,
    )

    return system, user
