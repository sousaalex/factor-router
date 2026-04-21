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

# Classifier system prompt — ensina o LLM sobre os modelos
CLASSIFIER_SYSTEM_PROMPT = """You are a model routing classifier for the Severino Agent.

YOUR JOB:
Given a user message, choose the BEST model from the list below.
Prefer LOCAL models (factorai/*) when possible — they cost $0.
Only use OpenRouter models (openrouter/*) when the task requires capabilities not available locally.

AVAILABLE MODELS:

LOCAL MODELS (factorai/*) — COST $0:
  • factorai/qwen3.6-35b-a3b
    - 35B parameters (3B active MoE), 128K context
    - Best for: complex reasoning, code generation, business logic, general tasks
    - Tool calls: 1-5
    - Use this as your DEFAULT choice for most tasks
    
  • factorai/qwen2.5:0.5b
    - 0.5B parameters, 32K context
    - Best for: simple greetings, basic classification, entity extraction
    - Tool calls: 0-2
    - Use only for VERY simple tasks (saudações, olá, bom dia, obrigado)

OPENROUTER MODELS (openrouter/*) — COST MONEY:
  • openrouter/qwen/qwen3.6-plus
    - Use when: factorai models are insufficient for complex coding tasks
    
  • openrouter/qwen/qwen3.5-plus-02-15
    - Use when: very long context (>128K) or complex vision analysis needed
    
  • openrouter/moonshotai/kimi-k2.5
    - Use when: many2one resolution, cross-entity synthesis, 5+ tool calls required
    
  • openrouter/xiaomi/mimo-v2-omni
    - Use when: video/audio processing, true multimodal (not just images)

DECISION RULES:
1. ALWAYS prefer factorai/* models (cost $0) unless the task clearly requires OpenRouter
2. For simple greetings (olá, bom dia, obrigado) → factorai/qwen2.5:0.5b
3. For most other tasks (code, business logic, reasoning) → factorai/qwen3.6-35b-a3b
4. Only use OpenRouter when the task explicitly requires capabilities not in factorai models

RESPONSE FORMAT:
Reply with ONLY: {{"model": "model_id"}}
Example: {{"model": "factorai/qwen3.6-35b-a3b"}}
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
    system = CLASSIFIER_SYSTEM_PROMPT
    if openrouter_balance_low:
        system = system + "\n" + LOW_BUDGET_BLOCK.strip() + "\n"

    user = CLASSIFIER_USER_PROMPT.format(
        user_message=user_message,
    )

    return system, user
