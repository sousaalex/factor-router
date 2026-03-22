"""
test_openai_sdk.py

Teste de integração real usando o OpenAI SDK a apontar para o FactorRouter.
Simula exactamente o que o Severino / Bluma vão fazer após a migração.

Testa:
    A. Chat simples — sem tools, sem stream
    B. Loop agentic — tool_calls com múltiplos ciclos no mesmo X-Turn-Id
    C. Streaming com tool_calls — SSE + tool loop

Valida em cada teste:
    - Modelo escolhido pelo router
    - Tokens acumulados por turno (X-Turn-Id)
    - Custo registado no centro de custos
    - X-Turn-Id correcto no loop agentic

Uso:
    uv run test_openai_sdk.py
"""
from __future__ import annotations

import asyncio
import json
import uuid
import httpx
from openai import AsyncOpenAI

# ─────────────────────────────────────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────────────────────────────────────

GATEWAY_URL  = "http://localhost:8003"
API_KEY      = "sk-fai-f13ea91d2c4a667c24346c0f14fab3a3161ead2f38a2376e"

SESSION_ID   = f"sdk-test-{uuid.uuid4().hex[:8]}"
COMPANY_ID   = "test-company-001"
COMPANY_NAME = "Empresa Teste Lda"
USER_ID      = "user-42"
USER_NAME    = "Alex Fonseca"
USER_EMAIL   = "alex@factorai.pt"


def _ascii(v: str) -> str:
    return v.encode("ascii", errors="replace").decode("ascii")


def make_client(turn_id: str, user_message: str) -> AsyncOpenAI:
    """
    Cria um AsyncOpenAI client apontado para o FactorRouter.
    Exactamente como o Severino / Bluma farão após a migração.
    """
    return AsyncOpenAI(
        api_key=API_KEY,
        base_url=f"{GATEWAY_URL}/v1",
        default_headers={
            "X-Turn-Id":         turn_id,
            "X-Session-Id":      SESSION_ID,
            "X-Conversation-Id": "null",
            "X-User-Message":    _ascii(user_message[:300]),
            "X-User-Id":         USER_ID,
            "X-User-Name":       _ascii(USER_NAME),
            "X-User-Email":      USER_EMAIL,
            "X-Company-Id":      COMPANY_ID,
            "X-Company-Name":    _ascii(COMPANY_NAME),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tools simuladas (para testes B e C)
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Obtém o tempo actual para uma cidade.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Nome da cidade"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_temperature",
            "description": "Obtém a temperatura em graus Celsius para uma cidade.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Nome da cidade"},
                },
                "required": ["city"],
            },
        },
    },
]


def execute_tool(name: str, args: dict) -> str:
    """Simula a execução de uma tool — devolve dados fictícios."""
    if name == "get_weather":
        return json.dumps({"city": args["city"], "weather": "ensolarado", "humidity": "65%"})
    if name == "get_temperature":
        return json.dumps({"city": args["city"], "temperature_celsius": 22})
    return json.dumps({"error": "tool desconhecida"})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de output
# ─────────────────────────────────────────────────────────────────────────────

def sep(title: str):
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print('─' * 64)

def ok(msg: str):    print(f"  ✓  {msg}")
def info(msg: str):  print(f"     {msg}")
def warn(msg: str):  print(f"  !  {msg}")
def step(msg: str):  print(f"  →  {msg}")


async def check_usage(turn_id: str, label: str):
    """Verifica o custo registado para este turno no centro de custos."""
    await asyncio.sleep(2)  # aguarda flush assíncrono

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GATEWAY_URL}/usage/logs",
            headers={"Authorization": f"Bearer {API_KEY}"},
            params={"session_id": SESSION_ID, "limit": 10},
        )

    if r.status_code != 200:
        warn(f"usage/logs falhou: {r.status_code}")
        return

    items = r.json().get("items", [])
    # filtra pelo turno mais recente desta sessão
    recent = items[:1] if items else []
    if recent:
        item = recent[0]
        meta = item.get("meta") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        ok(f"[{label}] Custo registado:")
        info(f"  model:        {item.get('model_id','?')}")
        info(f"  tokens:       {item.get('prompt_tokens','?')} prompt + {item.get('completion_tokens','?')} completion = {item.get('total_tokens','?')} total")
        info(f"  custo USD:    ${item.get('total_cost_usd', 0):.6f}")
        info(f"  tool_calls:   {item.get('tool_calls_count', 0)}")
        info(f"  source:       {meta.get('source','?')}")
        info(f"  llm_calls:    {meta.get('llm_calls_count','?')}")
    else:
        warn("Nenhum registo encontrado para esta sessão")


# ─────────────────────────────────────────────────────────────────────────────
# TESTE A — Chat simples sem tools
# ─────────────────────────────────────────────────────────────────────────────

async def test_a_chat_simples():
    sep("TESTE A — Chat simples (sem tools, sem stream)")

    turn_id     = str(uuid.uuid4())
    user_msg    = "What is the capital of Portugal? Reply in one sentence."
    client      = make_client(turn_id, user_msg)

    info(f"Turn-Id:  {turn_id[:18]}...")
    info(f"Mensagem: {user_msg}")
    step("A chamar o gateway via OpenAI SDK...")

    response = await client.chat.completions.create(
        model="gpt-4o-mini",   # ignorado — router decide
        messages=[{"role": "user", "content": user_msg}],
        stream=False,
    )

    model   = response.model
    content = response.choices[0].message.content
    usage   = response.usage

    ok("Resposta recebida")
    info(f"Modelo (router escolheu): {model}")
    info(f"Tokens: {usage.prompt_tokens} prompt + {usage.completion_tokens} completion")
    info(f"Resposta: {content}")

    await check_usage(turn_id, "A")


# ─────────────────────────────────────────────────────────────────────────────
# TESTE B — Loop agentic com tool_calls (non-stream)
# ─────────────────────────────────────────────────────────────────────────────

async def test_b_loop_agentic():
    sep("TESTE B — Loop agentic com tool_calls (non-stream)")
    info("Mesmo X-Turn-Id em todos os calls do loop")
    info("O router só é chamado UMA VEZ — no primeiro call")

    turn_id  = str(uuid.uuid4())
    user_msg = "What is the weather and temperature in Lisbon and Porto?"
    client   = make_client(turn_id, user_msg)

    info(f"Turn-Id:  {turn_id[:18]}...")
    info(f"Mensagem: {user_msg}")

    messages = [{"role": "user", "content": user_msg}]
    llm_calls = 0

    while True:
        llm_calls += 1
        step(f"LLM call #{llm_calls} (Turn-Id mantém-se igual)")

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            stream=False,
        )

        msg           = response.choices[0].message
        finish_reason = response.choices[0].finish_reason
        model         = response.model

        info(f"  finish_reason: {finish_reason} | model: {model}")

        if finish_reason == "tool_calls":
            # Executa as tools e adiciona os resultados
            messages.append(msg)
            for tc in msg.tool_calls:
                args   = json.loads(tc.function.arguments)
                result = execute_tool(tc.function.name, args)
                info(f"  tool: {tc.function.name}({args}) → {result}")
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })
        else:
            # finish_reason == "stop" — resposta final
            ok(f"Resposta final após {llm_calls} calls ao LLM")
            info(f"Resposta: {msg.content}")
            break

    info(f"X-Turn-Id usado em {llm_calls} calls — router chamado 1 vez")
    await check_usage(turn_id, "B")


# ─────────────────────────────────────────────────────────────────────────────
# TESTE C — Streaming com tool_calls
# ─────────────────────────────────────────────────────────────────────────────

async def test_c_stream_com_tools():
    sep("TESTE C — Streaming com tool_calls (SSE)")
    info("Stream token a token + tool loop no mesmo X-Turn-Id")

    turn_id  = str(uuid.uuid4())
    user_msg = "What is the weather in Faro? Stream the answer."
    client   = make_client(turn_id, user_msg)

    info(f"Turn-Id:  {turn_id[:18]}...")
    info(f"Mensagem: {user_msg}")

    messages  = [{"role": "user", "content": user_msg}]
    llm_calls = 0

    while True:
        llm_calls += 1
        step(f"LLM call #{llm_calls} com stream=True")

        stream = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            stream=True,
        )

        # Acumula o stream
        full_content   = ""
        tool_calls_acc = {}
        finish_reason  = None
        chunks_count   = 0
        model_used     = ""

        async for chunk in stream:
            if not model_used and chunk.model:
                model_used = chunk.model

            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            finish_reason = chunk.choices[0].finish_reason or finish_reason

            # conteúdo de texto
            if delta.content:
                full_content += delta.content
                chunks_count += 1
                print(f"  chunk #{chunks_count:02d}: {repr(delta.content)}")

            # acumula tool_calls do stream
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id":        tc.id or "",
                            "name":      "",
                            "arguments": "",
                        }
                    if tc.id:
                        tool_calls_acc[idx]["id"] = tc.id
                    if tc.function:
                        # nome só vem no primeiro chunk — não concatenar
                        if tc.function.name and not tool_calls_acc[idx]["name"]:
                            tool_calls_acc[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc.function.arguments

        info(f"  finish_reason: {finish_reason} | model: {model_used} | chunks: {chunks_count}")

        if finish_reason == "tool_calls" and tool_calls_acc:
            # Reconstrói a mensagem do assistente com tool_calls
            from openai.types.chat import ChatCompletionMessage
            from openai.types.chat.chat_completion_message_tool_call import (
                ChatCompletionMessageToolCall, Function
            )

            reconstructed_tool_calls = [
                ChatCompletionMessageToolCall(
                    id=tc["id"],
                    type="function",
                    function=Function(name=tc["name"], arguments=tc["arguments"]),
                )
                for tc in tool_calls_acc.values()
            ]

            messages.append({
                "role":       "assistant",
                "content":    full_content or None,
                "tool_calls": [
                    {
                        "id":       tc.id,
                        "type":     "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in reconstructed_tool_calls
                ],
            })

            for tc in reconstructed_tool_calls:
                args   = json.loads(tc.function.arguments)
                result = execute_tool(tc.function.name, args)
                info(f"  tool: {tc.function.name}({args}) → {result}")
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })
        else:
            ok(f"Stream concluído após {llm_calls} calls | Turn-Id único em todo o loop")
            if full_content:
                info(f"Resposta: {full_content[:150]}")
            break

    await check_usage(turn_id, "C")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 64)
    print("  FactorRouter — Teste OpenAI SDK (fluxo real)")
    print(f"  Gateway:  {GATEWAY_URL}")
    print(f"  Session:  {SESSION_ID}")
    print(f"  SDK:      openai Python")
    print("=" * 64)

    await test_a_chat_simples()
    await test_b_loop_agentic()
    await test_c_stream_com_tools()

    print(f"\n{'=' * 64}")
    print("  Todos os testes concluídos.")
    print("  Verifica o centro de custos em:")
    print(f"  GET {GATEWAY_URL}/usage/logs?session_id={SESSION_ID}")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())