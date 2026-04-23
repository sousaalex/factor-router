#!/usr/bin/env python3
"""
Script manual para testar o provider FactorAI vLLM local.

Uso:
    python scripts/test-factorai-model.py

Requisitos:
    - FactorRouter a correr em http://localhost:8003
    - API key válida em FACTOR_ROUTER_KEY (variável de ambiente)
    - vLLM configurado com FACTORAI_VLLM_BASE_URL no .env
"""
import os
import sys
from openai import OpenAI


def test_factorai_model(
    model: str = "factorai/qwen3.6-35b-a3b",
    base_url: str = "http://localhost:8003/v1",
    api_key: str | None = None,
):
    """
    Testa um call ao FactorRouter com modelo FactorAI.

    Args:
        model: Model ID (ex: factorai/qwen3.6-35b-a3b ou factorai/qwen2.5:0.5b)
        base_url: FactorRouter URL (default: http://localhost:8003/v1)
        api_key: Factor Router API key (default: FACTOR_ROUTER_KEY env var)
    """
    api_key = api_key or os.getenv("FACTOR_ROUTER_KEY")
    if not api_key:
        print("❌ Erro: FACTOR_ROUTER_KEY não definida")
        print("   Define a variável de ambiente ou passa --api-key")
        sys.exit(1)

    print(f"🔍 A testar modelo: {model}")
    print(f"📍 FactorRouter: {base_url}")
    print(f"🔑 API Key: {api_key[:12]}...{api_key[-8:]}")
    print()

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
    )

    messages = [
        {"role": "user", "content": "Olá! Qual é o teu nome?"}
    ]

    print("📤 A enviar request...")
    print()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
        )

        print("📥 Resposta (streaming):")
        print("-" * 50)

        full_content = ""
        for chunk in response:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                print(content, end="", flush=True)
                full_content += content

        print()
        print("-" * 50)
        print()
        print(f"✅ Sucesso! {len(full_content)} caracteres recebidos")

        # Tenta extrair usage do último chunk
        if hasattr(chunk, "usage") and chunk.usage:
            print(f"📊 Usage: {chunk.usage}")

    except Exception as e:
        print(f"❌ Erro: {e}")
        print()
        print("Possíveis causas:")
        print("  1. FactorRouter não está a correr")
        print("  2. API key inválida")
        print("  3. Modelo não configurado no FactorRouter")
        print("  4. vLLM indisponível")
        sys.exit(1)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Testa modelo FactorAI vLLM via FactorRouter"
    )
    parser.add_argument(
        "--model",
        default="factorai/qwen3.6-35b-a3b",
        help="Model ID (default: factorai/qwen3.6-35b-a3b)",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8003/v1",
        help="FactorRouter URL (default: http://localhost:8003/v1)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Factor Router API key (default: FACTOR_ROUTER_KEY env var)",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="Lista modelos FactorAI disponíveis",
    )

    args = parser.parse_args()

    if args.list_models:
        print("Modelos FactorAI disponíveis:")
        print("  - factorai/qwen3.6-35b-a3b (reasoning, 35B MoE)")
        print("  - factorai/qwen2.5:0.5b (simple, 0.5B)")
        print()
        print("Usa --model para escolher:")
        print("  python scripts/test-factorai-model.py --model factorai/qwen2.5:0.5b")
        return

    test_factorai_model(
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
    )


if __name__ == "__main__":
    main()
