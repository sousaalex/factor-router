# FactorRouter

> Gateway centralizado de LLM para a FactorAI — routing inteligente, gestão empresarial de API Keys e centro de custos unificado.

FactorRouter é um proxy OpenAI-compatível que fica entre as tuas apps e os providers de LLM. As apps deixam de chamar o OpenRouter diretamente e passam a usar o FactorRouter como único ponto de entrada — com duas linhas de código.

```
[Severino AgiWeb]  ──┐
[Severino WA]      ──┤──▶  FactorRouter :8003  ──▶  OpenRouter  ──▶  LLM
[Bluma npm]        ──┘             │
                             Postgres
                       (apps · keys · usage)
```

---

## Índice

1. [Funcionalidades](#funcionalidades)
2. [Arquitectura](#arquitectura)
3. [Início Rápido](#início-rápido)
4. [Variáveis de Ambiente](#variáveis-de-ambiente)
5. [Gestão de Apps e API Keys](#gestão-de-apps-e-api-keys)
6. [Integração nas Apps](#integração-nas-apps)
7. [Headers Obrigatórios](#headers-obrigatórios)
8. [Endpoints](#endpoints)
9. [Centro de Custos](#centro-de-custos)
10. [Router de Modelos](#router-de-modelos)
11. [Segurança](#segurança)
12. [Base de Dados](#base-de-dados)
13. [Desenvolvimento](#desenvolvimento)
14. [Testes](#testes)

---

## Funcionalidades

- **Drop-in replacement** para o OpenAI SDK — muda `base_url` e `api_key`, o resto fica igual
- **Routing automático de modelos** via Ollama local (custo $0) — escolhe o modelo mais adequado e económico para cada mensagem
- **Gestão empresarial de API Keys** — SHA-256 hash, nunca a key real no DB, revogação imediata
- **Centro de custos unificado** — registo automático de tokens, custo e contexto por turno agentic
- **Acumulação por turno** — múltiplos calls ao LLM dentro do mesmo loop agentic (tool_calls) registados como 1 único turno
- **Isolamento de dados por app** — cada app só vê os seus próprios logs de uso
- **Admin API** — criar apps, gerar/revogar keys, listar uso
- **OpenAI-compatible SSE** — streaming token-a-token transparente
- **Docker Compose** — pronto a correr com um único comando

---

## Arquitectura

```
┌─────────────────────────────────────────────────────────────────┐
│                        FactorRouter                              │
│                                                                  │
│  POST /v1/chat/completions                                       │
│       │                                                          │
│       ├─ auth.py          valida Bearer key (SHA-256 + cache)   │
│       ├─ context.py       valida 9 headers X-*                  │
│       ├─ accumulator.py   abre/reutiliza balde por X-Turn-Id    │
│       ├─ router.py        escolhe model_id (Ollama, 1x/turno)   │
│       └─ proxy.py         SSE ou JSON ao OpenRouter             │
│              │                                                   │
│              └─ flush assíncrono ──▶ usage/service.py           │
│                                            │                     │
│                                       llm_usage_log             │
│                                       (Postgres)                 │
└─────────────────────────────────────────────────────────────────┘
```

### Fluxo por request

```
1. Valida API Key (SHA-256 → cache em memória → app_id)
2. Valida 9 headers X-*
3. X-Turn-Id novo?
   → sim: chama router Ollama UMA VEZ → abre balde de tokens
   → não: usa model_id do balde (router ignorado)
4. Injeta model_id + stream_options no body
5. Proxy ao OpenRouter (SSE ou JSON)
6. Extrai tokens de cada chunk
7. finish_reason=tool_calls → mantém balde aberto (agente vai fazer outro call)
   finish_reason=stop       → flush assíncrono → 1 linha no Postgres
```

---

## Início Rápido

### Pré-requisitos

- Docker + Docker Compose
- Ollama a correr no host com pelo menos um modelo leve (ex: `qwen2.5:0.5b`)
- Conta no [OpenRouter](https://openrouter.ai)

### 1. Clona e configura

```bash
git clone <repo> factor_router
cd factor_router

cp .env.example .env
# edita o .env com os valores reais (ver secção Variáveis de Ambiente)
```

### 2. Configura o Ollama para aceitar ligações Docker

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d/
sudo tee /etc/systemd/system/ollama.service.d/override.conf << EOF
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

### 3. Arranca

```bash
docker compose up -d
```

As migrações SQL correm automaticamente no primeiro arranque do Postgres.

### 4. Verifica

```bash
curl http://localhost:8003/health
# {"status": "ok", "version": "2.0.0", "upstream": "https://openrouter.ai/api/v1"}
```

### 5. Regista a primeira app e gera uma key

```bash
# Cria a app (access token Auth0 com audience da API — ver Auth0 Dashboard)
curl -X POST http://localhost:8003/admin/apps \
  -H "Authorization: Bearer <AUTH0_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Minha App", "description": "Descrição opcional"}'

# Gera a key — guarda o valor api_key, não é mostrado novamente
curl -X POST http://localhost:8003/admin/apps/minha-app/keys \
  -H "Authorization: Bearer <AUTH0_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"label": "production"}'
```

---

## Variáveis de Ambiente

Um único `.env` para o gateway e para o Postgres.

| Variável | Obrigatória | Descrição |
|---|---|---|
| `POSTGRES_USER` | ✓ | Utilizador do Postgres |
| `POSTGRES_PASSWORD` | ✓ | Password do Postgres |
| `POSTGRES_DB` | ✓ | Nome da base de dados |
| `DATABASE_URL` | ✓ | `postgresql://user:pass@postgres:5432/db` |
| `OPENROUTER_API_KEY` | ✓ | Key do OpenRouter — nunca sai do gateway |
| `AUTH0_DOMAIN` | ✓ | Tenant Auth0 (ex: `dev-xxx.eu.auth0.com`) |
| `AUTH0_AUDIENCE` | ✓ | Identifier da API Auth0 (audience do access token) |
| `AUTH0_ISSUER` | — | Opcional; default `https://<AUTH0_DOMAIN>/` |
| `AUTH0_JWT_LEEWAY_SECONDS` | — | Default `0` |
| `OLLAMA_BASE_URL` | ✓ | `http://host.docker.internal:11434` |
| `CLASSIFIER_MODEL` | ✓ | Modelo Ollama para classificação (ex: `qwen2.5:0.5b`) |
| `UPSTREAM_URL` | — | Default: `https://openrouter.ai/api/v1` |
| `UPSTREAM_TIMEOUT` | — | Default: `120` segundos |
| `CLASSIFIER_TIMEOUT_SECONDS` | — | Default: `6.0` segundos |
| `HOST` | — | Default: `0.0.0.0` |
| `PORT` | — | Default: `8003` |
| `LOG_LEVEL` | — | Default: `info` |

> **Admin:** access token com audience = `AUTH0_AUDIENCE` e **todas** as permissões definidas em `ADMIN_GATEWAY_REQUIRED_PERMISSIONS` em `src/gateway/auth0_admin.py` (`create` / `delete` / `read` / `update` : `admin-factorai`).

---

## Gestão de Apps e API Keys

### Como funciona

Cada aplicação tem um `app_id` (gerado automaticamente do nome) e pode ter múltiplas API Keys. A key real **nunca é guardada** — apenas o seu SHA-256 hash. Se a base de dados for comprometida, nenhuma key é recuperável.

```
Nome fornecido: "Severino WhatsApp"
app_id gerado:  "severino-whatsapp"

Key gerada:   sk-fai-e5627b264cf469b5d8dbe06c415dcf74a2f36947c61ce131
Hash no DB:   sha256("sk-fai-e5627b...") = "a3f9c2d1e4b5..."  ← só isto fica no Postgres
```

### Endpoints Admin

Todos os endpoints `/admin/*` requerem `Authorization: Bearer <access_token Auth0>`.

#### Criar app

```bash
POST /admin/apps
Authorization: Bearer <access_token>

{
  "name": "Severino WhatsApp",
  "description": "Agente WhatsApp via Evolution API"
}
```

```json
{
  "id": "uuid...",
  "app_id": "severino-whatsapp",
  "name": "Severino WhatsApp",
  "is_active": true,
  "created_at": "2026-03-20T..."
}
```

#### Gerar key

```bash
POST /admin/apps/{app_id}/keys
Authorization: Bearer <access_token>

{"label": "production"}
```

```json
{
  "api_key":    "sk-fai-e5627b264cf469b5d8dbe06c415dcf74a2f36947c61ce131",
  "key_id":     "uuid...",
  "key_prefix": "sk-fai-e5627b",
  "app_id":     "severino-whatsapp",
  "label":      "production",
  "warning":    "Store this key securely — it will not be shown again."
}
```

> ⚠️ A `api_key` é devolvida **uma única vez**. Guarda-a imediatamente.

#### Listar apps

```bash
GET /admin/apps
Authorization: Bearer <access_token>
```

#### Listar keys de uma app

```bash
GET /admin/apps/{app_id}/keys
Authorization: Bearer <access_token>
```

Devolve apenas `key_prefix`, `label`, `last_used_at`, `created_at`, `revoked_at` — nunca o hash.

#### Revogar key

```bash
DELETE /admin/apps/{app_id}/keys/{key_id}
Authorization: Bearer <access_token>
```

Efeito imediato — o cache em memória é invalidado e a key deixa de ser aceite em segundos. O registo fica no Postgres com `revoked_at` preenchido (audit trail).

#### Rotação de keys sem downtime

```bash
# 1. Gera nova key
POST /admin/apps/{app_id}/keys {"label": "v2"}

# 2. Atualiza a variável de ambiente da app para a nova key
# 3. Reinicia a app

# 4. Revoga a key antiga
DELETE /admin/apps/{app_id}/keys/{key_id_antigo}
```

---

## Integração nas Apps

A migração é feita em **duas linhas** — muda `api_key` e `base_url`. O resto do código fica igual.

```python
import os, uuid
from openai import AsyncOpenAI

def make_llm_client(
    turn_id: str,
    session_id: str,
    user_message: str,
    conversation_id: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    user_email: str | None = None,
    company_id: str | None = None,
    company_name: str | None = None,
) -> AsyncOpenAI:

    def _ascii(v: str | None) -> str:
        if not v:
            return "null"
        return v.encode("ascii", errors="replace").decode("ascii")

    return AsyncOpenAI(
        api_key=os.getenv("FACTOR_ROUTER_KEY"),
        base_url=os.getenv("FACTOR_ROUTER_URL") + "/v1",
        default_headers={
            "X-Turn-Id":         turn_id,
            "X-Session-Id":      session_id,
            "X-Conversation-Id": conversation_id or "null",
            "X-User-Message":    _ascii(user_message[:300]),
            "X-User-Id":         str(user_id) if user_id else "null",
            "X-User-Name":       _ascii(user_name),
            "X-User-Email":      user_email or "null",
            "X-Company-Id":      str(company_id) if company_id else "null",
            "X-Company-Name":    _ascii(company_name),
        },
    )


# Uso no agente — turno agentic completo com tool_calls
async def run_turn(user_message: str, session_id: str, ...):
    turn_id = str(uuid.uuid4())  # ← gerar UMA VEZ por turno
    client  = make_llm_client(turn_id, session_id, user_message, ...)

    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await client.chat.completions.create(
            model="auto",  # ignorado — FactorRouter decide qual LLM responde
            messages=messages,
            tools=TOOLS,
            stream=False,
        )

        msg = response.choices[0].message
        if response.choices[0].finish_reason == "tool_calls":
            messages.append(msg)
            for tc in msg.tool_calls:
                result = execute_tool(tc.function.name, json.loads(tc.function.arguments))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            return msg.content  # resposta final
```

### Variáveis de ambiente a adicionar

```bash
FACTOR_ROUTER_URL=http://localhost:8003
FACTOR_ROUTER_KEY=sk-fai-...
```

### O que remover das apps

```
router.py, classifier_prompt.py, models_config.yaml  ← gateway decide o modelo
llm_usage_service.py, llm_usage_log.py               ← gateway regista automaticamente
Acumulador de tokens (_usage_acc_by_session)          ← gateway acumula por X-Turn-Id
Chamadas a record_turn_usage()                        ← gateway grava no DB
stream_options={"include_usage": True}                ← gateway injeta automaticamente
OPENROUTER_API_KEY                                    ← substituída por FACTOR_ROUTER_KEY
```

---

## Headers Obrigatórios

O gateway valida **9 headers X-*** em cada request ao `/v1/chat/completions`. Header ausente devolve `400` com o nome do header em falta.

| Header | Nullable | Descrição |
|---|---|---|
| `X-Turn-Id` | ❌ | UUID v4 gerado no início de cada turno. Deve ser **o mesmo** em todos os calls do loop agentic (tool_calls, tool_results). |
| `X-Session-Id` | ❌ | ID da sessão de chat |
| `X-User-Message` | ❌ | Primeiros 300 chars da mensagem do utilizador |
| `X-Conversation-Id` | ✓ | ID da conversa. Envia `"null"` se desconhecido. |
| `X-User-Id` | ✓ | ID do utilizador. Envia `"null"` se desconhecido. |
| `X-User-Name` | ✓ | Nome do utilizador. Envia `"null"` se desconhecido. |
| `X-User-Email` | ✓ | Email do utilizador. Envia `"null"` se desconhecido. |
| `X-Company-Id` | ✓ | ID da empresa. Envia `"null"` se desconhecido. |
| `X-Company-Name` | ✓ | Nome da empresa. Envia `"null"` se desconhecido. |

> **Regra:** header ausente = `400`. Valor desconhecido = enviar `"null"` como string, nunca omitir.

> **Nota:** O `app_id` **não é um header** — é inferido automaticamente da API Key. Não pode ser declarado nem falsificado pelo agente.

> **ASCII:** os valores dos headers devem ser ASCII puro. Para strings com caracteres portugueses (ã, é, ç...) fazer encode: `value.encode("ascii", errors="replace").decode("ascii")`.

---

## Endpoints

### Público

| Método | Path | Auth | Descrição |
|---|---|---|---|
| `GET` | `/health` | — | Estado do gateway |
| `GET` | `/docs` | — | Swagger UI |

### LLM Proxy

| Método | Path | Auth | Descrição |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | Bearer key | Proxy OpenAI-compatible |

### Centro de Custos

| Método | Path | Auth | Descrição |
|---|---|---|---|
| `GET` | `/usage/logs` | Bearer API key / Bearer Auth0 | Logs de uso por turno |
| `GET` | `/usage/stats` | Bearer API key / Bearer Auth0 | Agregados por modelo e app |

> **Isolamento:** com Bearer API key, a app só vê os seus próprios dados — `app_id` é forçado pela key, o parâmetro `?app_id=` é ignorado. Com Bearer Auth0 (access token JWS), vês tudo sem filtros de app.

### Admin

| Método | Path | Auth | Descrição |
|---|---|---|---|
| `POST` | `/admin/apps` | Admin | Criar app |
| `GET` | `/admin/apps` | Admin | Listar apps |
| `POST` | `/admin/apps/{id}/keys` | Admin | Gerar key |
| `GET` | `/admin/apps/{id}/keys` | Admin | Listar keys |
| `DELETE` | `/admin/apps/{id}/keys/{kid}` | Admin | Revogar key |

---

## Centro de Custos

### Estrutura de um registo

```json
{
  "id": 42,
  "created_at": "2026-03-20T20:06:39Z",
  "turn_id": "81865ea5-b886-4ba7-94d1-c8a171178c19",
  "app_id": "severino-agiweb",
  "chat_session_id": "session-abc",
  "user_id": "42",
  "user_name": "Alex Fonseca",
  "user_email": "alex@factorai.pt",
  "company_id": "4",
  "company_name": "BOLTHERM LDA",
  "conversation_id": null,
  "user_message": "Qual o saldo da conta corrente?",
  "model_id": "openai/gpt-4o-mini",
  "prompt_tokens": 379,
  "completion_tokens": 156,
  "total_tokens": 535,
  "input_price_per_1m": 0.15,
  "output_price_per_1m": 0.60,
  "input_cost_usd": 0.000057,
  "output_cost_usd": 0.000094,
  "total_cost_usd": 0.000151,
  "tool_calls_count": 4,
  "meta": {
    "source": "usage_real",
    "llm_calls_count": 2
  }
}
```

### Queries úteis

```bash
# Logs da minha app (últimos 50)
GET /usage/logs
Authorization: Bearer sk-fai-...

# Logs de uma empresa específica
GET /usage/logs?company_id=4&limit=100
Authorization: Bearer sk-fai-...

# Logs num intervalo de datas
GET /usage/logs?date_from=2026-03-01&date_to=2026-03-31
Authorization: Bearer sk-fai-...

# Estatísticas — custo total + breakdown por modelo
GET /usage/stats
Authorization: Bearer sk-fai-...

# Admin — ver tudo, filtrar por app
GET /usage/logs?app_id=severino-agiweb
Authorization: Bearer <access_token_auth0>

# Correlacionar um turno específico (via psql)
SELECT * FROM llm_usage_log WHERE turn_id = 'uuid-do-turno';
```

---

## Router de Modelos

O router usa um modelo Ollama local (gratuito) para classificar cada mensagem e escolher o modelo LLM mais adequado de `models_config.yaml`.

### Como funciona

```
Mensagem: "Qual o saldo da conta corrente?"
       ↓
Ollama (qwen2.5:0.5b) classifica → complexidade: baixa, tipo: consulta
       ↓
models_config.yaml → modelo barato escolhido (ex: gpt-4o-mini)
       ↓
X-Turn-Id guardado com model_id → próximos calls do mesmo turno usam mesmo modelo
```

### Configuração

```yaml
# models_config.yaml
default_model: openai/gpt-4o-mini

models:
  - id: openai/gpt-4o-mini
    description: Consultas simples, respostas directas
    input_per_1m_tokens: 0.15
    output_per_1m_tokens: 0.60

  - id: openai/gpt-4o
    description: Análise complexa, raciocínio multi-passo
    input_per_1m_tokens: 2.50
    output_per_1m_tokens: 10.00
```

### Fallback

Se o Ollama não estiver disponível ou exceder o timeout, o gateway usa o `default_model` automaticamente. O agente nunca recebe um erro por falha do router.

---

## Segurança

### API Keys

| Propriedade | Detalhe |
|---|---|
| Formato | `sk-fai-{48 hex chars}` — 192 bits de entropia |
| Armazenamento | Apenas SHA-256(key) no Postgres — a key real nunca fica no DB |
| Validação | `sha256(key)` → lookup em cache memória → O(1), zero I/O |
| Cache | TTL 5 minutos — recarrega do Postgres automaticamente |
| Revogação | Imediata — cache invalidado no momento da revogação |
| Rotação | Múltiplas keys por app — rotação sem downtime |

### Isolamento

- Cada app só pode ver os seus próprios logs de uso — `app_id` é sempre inferido da API Key
- Uma app não pode declarar um `app_id` diferente — o header `X-App-Id` não existe
- Admin API protegida por JWT Auth0 (`Authorization: Bearer`), separado das API Keys das apps
- Postgres acessível na porta host `5431` e na rede Docker `router_net` (hostname do serviço: `router-db`)

### Boas práticas

```bash
# Nunca commitar o .env
echo ".env" >> .gitignore

# Gerar valores seguros
python3 -c "import secrets; print(secrets.token_hex(16))"  # POSTGRES_PASSWORD

# Revogar keys comprometidas imediatamente
DELETE /admin/apps/{app_id}/keys/{key_id}
```

---

## Base de Dados

### Esquema

```
gateway_apps
  id            uuid PK
  app_id        text UNIQUE        "severino-agiweb"
  name          text               "Severino AgiWeb"
  description   text
  is_active     boolean
  created_at    timestamptz
  updated_at    timestamptz

gateway_api_keys
  id            uuid PK
  app_id        text FK → gateway_apps
  key_hash      text UNIQUE        SHA-256(key_real)
  key_prefix    text               "sk-fai-e5627b"
  label         text               "production"
  is_active     boolean
  last_used_at  timestamptz
  created_at    timestamptz
  revoked_at    timestamptz        audit trail

llm_usage_log
  id                  bigserial PK
  created_at          timestamptz
  turn_id             text               X-Turn-Id do agente
  app_id              text
  chat_session_id     text               X-Session-Id
  user_id/name/email  text               X-User-*
  company_id/name     text               X-Company-*
  conversation_id     text               X-Conversation-Id
  user_message        text               primeiros 500 chars
  model_id            text               escolhido pelo router
  prompt_tokens       integer            acumulado por turn_id
  completion_tokens   integer            acumulado por turn_id
  total_tokens        integer
  input_price_per_1m  numeric            snapshot do momento
  output_price_per_1m numeric            snapshot do momento
  input_cost_usd      numeric
  output_cost_usd     numeric
  total_cost_usd      numeric
  tool_calls_count    integer            total de tools no turno
  meta                jsonb              source, llm_calls_count
```

### Migrações

```bash
# Aplicar manualmente (usa POSTGRES_USER e POSTGRES_DB do teu .env)
set -a && source .env && set +a
docker exec -i router-db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - \
  < migrations/001_gateway_apps.sql

docker exec -i router-db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - \
  < migrations/002_llm_usage_log.sql

docker exec -i router-db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - \
  < migrations/003_add_turn_id_to_llm_usage_log.sql
```

> O `docker-compose` atual **não** monta volumes: não há persistência nem execução automática de `./migrations/`. Corre os SQL acima após o primeiro `up` (ou sempre que recriares o container sem dados).

---

## Desenvolvimento

### Estrutura do projecto

```
factor_router/
├── src/
│   ├── api/
│   │   ├── app.py                FastAPI app, lifespan, middleware
│   │   └── routes/
│   │       ├── admin.py          Admin API
│   │       ├── health.py         GET /health
│   │       ├── proxy.py          POST /v1/chat/completions
│   │       └── usage.py          GET /usage/logs e /stats
│   ├── gateway/
│   │   ├── accumulator.py        Acumulador de tokens por X-Turn-Id
│   │   ├── auth.py               Validação de Bearer key
│   │   ├── config.py             Settings via Pydantic
│   │   ├── context.py            Extração e validação de headers X-*
│   │   ├── key_store.py          SHA-256 store + cache em memória
│   │   └── proxy.py              Proxy SSE e JSON ao OpenRouter
│   ├── router/
│   │   ├── router.py             Classificação via Ollama
│   │   └── models_config.yaml    Catálogo de modelos e preços
│   └── usage/
│       └── service.py            record/read de uso no Postgres
├── migrations/
│   ├── 001_gateway_apps.sql
│   ├── 002_llm_usage_log.sql
│   └── 003_add_turn_id_to_llm_usage_log.sql
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── run.py
```

### Correr localmente (sem Docker)

```bash
uv sync
cp .env.example .env
# editar .env — fora do Docker: `localhost:5431`; dentro da stack: host `router-db`, porta `5432`

uv run run.py
```

### Rebuild após mudanças de dependências

```bash
# Sempre que mudas o pyproject.toml
uv lock
docker compose up -d --build --force-recreate
```

### Logs em tempo real

```bash
docker logs -f router-api
docker logs -f router-db
```

### Aceder ao Postgres

```bash
set -a && source .env && set +a   # ou exporta POSTGRES_USER e POSTGRES_DB à mão
docker exec -it router-db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

---

## Testes

Os ficheiros de teste estão em **`test/`**. Relatórios gerados (coverage HTML, logs) ficam em **`test/result/`**.

```bash
# Dependências + coverage (opcional)
uv sync --extra dev

# Unitários (unittest) — router, créditos OpenRouter, política Claude / Qwen Plus
uv run python -m unittest discover -s test -v

# Unitários + cobertura HTML + test_report.html em test/result/
chmod +x test/run_tests.sh
./test/run_tests.sh

# Integração manual — gateway a correr (auth, headers, proxy, usage)
uv run python test/test_gateway.py

# OpenAI SDK — fluxos reais: chat, tool_calls, streaming
uv run python test/test_openai_sdk.py
```

### Configuração dos testes

No topo de cada script:

```python
GATEWAY_URL  = "http://localhost:8003"
API_KEY      = "sk-fai-..."       # key gerada via Admin API
```

### O que é testado

| Teste | Valida |
|---|---|
| Health check | Gateway a correr |
| Key inválida → 401 | Auth a funcionar |
| Header em falta → 400 | Validação de contexto |
| Chat non-stream | Proxy + router + usage |
| Chat SSE stream | Streaming token-a-token |
| Agentic loop non-stream | Acumulação por turn_id, flush no stop |
| Agentic loop SSE + tools | Stream + tool_calls + 1 registo |
| Usage logs | Registo correcto no Postgres |

---

## Licença

Proprietária — FactorAI © 2026. Todos os direitos reservados.