from openai import OpenAI
import time

# ---- CONFIG ----
MODEL = "gemma4:latest"

# preço simulado (ajusta como quiser)
PRICE_PER_1K_PROMPT = 0.0005
PRICE_PER_1K_COMPLETION = 0.0015

client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama"
)

messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Explica o que é uma LLM em 2 linhas."}
]

# ---- STREAMING ----
start_time = time.time()

stream = client.chat.completions.create(
    model=MODEL,
    messages=messages,
    stream=True,
    temperature=0.7
)

print("\nResposta:\n")

full_response = ""

for chunk in stream:
    if chunk.choices[0].delta.content:
        content = chunk.choices[0].delta.content
        print(content, end="", flush=True)
        full_response += content

end_time = time.time()

# ---- ESTIMATIVA DE TOKENS (simples) ----
def estimate_tokens(text):
    return int(len(text.split()) * 1.3)

prompt_text = " ".join([m["content"] for m in messages])

prompt_tokens = estimate_tokens(prompt_text)
completion_tokens = estimate_tokens(full_response)
total_tokens = prompt_tokens + completion_tokens

# ---- CUSTO SIMULADO ----
prompt_cost = (prompt_tokens / 1000) * PRICE_PER_1K_PROMPT
completion_cost = (completion_tokens / 1000) * PRICE_PER_1K_COMPLETION
total_cost = prompt_cost + completion_cost

# ---- METRICS ----
print("\n\n--- MÉTRICAS ---")
print(f"Tempo: {end_time - start_time:.2f}s")
print(f"Prompt tokens (estimado): {prompt_tokens}")
print(f"Completion tokens (estimado): {completion_tokens}")
print(f"Total tokens: {total_tokens}")

print("\n--- CUSTO (SIMULADO) ---")
print(f"Prompt: ${prompt_cost:.6f}")
print(f"Completion: ${completion_cost:.6f}")
print(f"Total: ${total_cost:.6f}")