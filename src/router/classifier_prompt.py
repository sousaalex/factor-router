"""
Factor AI — Classifier Prompt
-------------------------------
Builds system and user prompts for the routing classifier.
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

# Classifier system prompt — simplified to return tier only
CLASSIFIER_SYSTEM_PROMPT = """You are a model routing classifier for the Severino Agent.

Classify each request into ONE of these 4 tiers:

TIER 1 (simple): Greetings, thanks, simple ERP queries, single-entity lookups
  Examples: "olá", "bom dia", "total de vendas", "listar faturas", "telefone do cliente"

TIER 2 (reasoning): Coding tasks, long context >262K, vision+text analysis, multi-step linear operations
  Examples: "refactor código Python", "analisar documento grande", "imagem de gráfico"

TIER 3 (reasoning+): Many2one resolution WITH cross-entity synthesis, 2-8 tool calls
  Examples: "criar fatura para cliente X com produtos do orçamento Y"

TIER 4 (complex): 5-12 tool calls with branching logic, cascading creation
  Examples: "criar cliente, contacto, projeto e tarefa com condicionais complexas"

RULES:
- Default to the LOWEST tier that can handle the task
- Only escalate when multi-entity orchestration is clearly required
- Reply with ONLY: {{"tier": 1}} or {{"tier": 2}} or {{"tier": 3}} or {{"tier": 4}}
- NO explanation, NO markdown, ONLY the JSON
"""

LOW_BUDGET_BLOCK = """
---
BUDGET MODE: Prefer tier 1 and 2. Only use tier 3 when cross-entity synthesis is clearly required.
---
"""

# User prompt — English, includes token estimates
CLASSIFIER_USER_PROMPT = """User message: "{user_message}"

Estimated tokens: input ~{est_input}, output ~{est_output}.

Reply with ONLY: {{"tier": N}} where N is 1, 2, 3, or 4."""


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
    est_total = estimated_input_tokens + estimated_output_tokens

    system = CLASSIFIER_SYSTEM_PROMPT
    if openrouter_balance_low:
        system = system + "\n" + LOW_BUDGET_BLOCK.strip() + "\n"

    user = CLASSIFIER_USER_PROMPT.format(
        user_message=user_message,
        est_input=estimated_input_tokens,
        est_output=estimated_output_tokens,
    )

    return system, user
