#!/usr/bin/env python3
"""
Debug script para o router - testa o classificador diretamente
"""
import asyncio
import os
import sys

# Adicionar o src ao path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.router.router import route, _call_classifier, _heuristic_route_model, _heuristic_is_confident, estimate_request_tokens
from src.router.classifier_prompt import build_classifier_prompt, build_models_description
from src.router.router import _MODELS, _DEFAULT_MODEL, OLLAMA_BASE_URL, CLASSIFIER_MODEL, CLASSIFIER_TIMEOUT

async def test_router():
    print("=" * 80)
    print("ROUTER DEBUG TEST")
    print("=" * 80)
    print(f"\nConfiguração:")
    print(f"  OLLAMA_BASE_URL: {OLLAMA_BASE_URL}")
    print(f"  CLASSIFIER_MODEL: {CLASSIFIER_MODEL}")
    print(f"  CLASSIFIER_TIMEOUT: {CLASSIFIER_TIMEOUT}")
    print(f"  ROUTER_DECISION_MODE: {os.getenv('ROUTER_DECISION_MODE', 'hybrid')}")
    print(f"  _DEFAULT_MODEL: {_DEFAULT_MODEL}")
    print()
    
    # Test messages
    test_messages = [
        "Olá, bom dia!",
        "Qual é o total de vendas deste mês?",
        "Cria uma fatura para o cliente BOLTHERM com os produtos do orçamento",
        "Preciso de analisar uma imagem de um gráfico de vendas",
        "Faz um refactor do código Python para melhorar a performance",
    ]
    
    for msg in test_messages:
        print("-" * 80)
        print(f"\nTest message: '{msg}'")
        print()
        
        # Test heuristic
        heuristic_model = _heuristic_route_model(msg)
        confident = _heuristic_is_confident(msg)
        print(f"  Heuristic result: {heuristic_model}")
        print(f"  Heuristic confident: {confident}")
        
        # Test classifier
        est_in, est_out = estimate_request_tokens(msg)
        print(f"  Estimated tokens: input={est_in}, output={est_out}")
        
        try:
            system_prompt, user_prompt = build_classifier_prompt(
                user_message=msg,
                models=_MODELS,
                default_model=_DEFAULT_MODEL,
                estimated_input_tokens=est_in,
                estimated_output_tokens=est_out,
                openrouter_balance_low=False,
            )
            
            content, inp, out, duration_ms = await _call_classifier(
                user_message=msg,
                est_in=est_in,
                est_out=est_out,
                base=OLLAMA_BASE_URL,
                openrouter_balance_low=False,
            )
            
            print(f"  Classifier response: '{content[:200]}'")
            print(f"  Classifier tokens: input={inp}, output={out}")
            print(f"  Classifier duration: {duration_ms}ms")
            
            # Parse the response
            from src.router.router import _parse_model_from_response
            model_id, fallback_reason = _parse_model_from_response(content)
            print(f"  Parsed model: {model_id}")
            print(f"  Fallback reason: {fallback_reason}")
            
        except Exception as e:
            print(f"  Classifier ERROR: {e}")
        
        # Test full route
        print()
        result = await route(msg, openrouter_balance_low=False)
        print(f"  FINAL RESULT: {result.model_id}")
        print(f"  Raw response: {result.raw_response[:100] if result.raw_response else 'None'}")
        print()

if __name__ == "__main__":
    asyncio.run(test_router())
