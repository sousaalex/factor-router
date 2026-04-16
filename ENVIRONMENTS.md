# Ambientes - FactorRouter

A API está separada em dois ambientes independentes: **Desenvolvimento** e **Produção**.

## Estrutura de Ficheiros

| Ficheiro | Ambiente | Descrição |
|----------|----------|-----------|
| `.env.dev` | Desenvolvimento | Variáveis de ambiente para dev |
| `.env.prod` | Produção | Variáveis de ambiente para prod |
| `docker-compose-dev.yml` | Desenvolvimento | Configuração Docker para dev |
| `docker-compose-prod.yml` | Produção | Configuração Docker para prod |

## Diferenças entre Ambientes

### Desenvolvimento (`.env.dev` + `docker-compose-dev.yml`)

- **Router**: `ROUTER_DECISION_MODE=llm` (sempre chama o classificador)
- **LOG_LEVEL**: `info` (mais detalhado)
- **DATABASE_URL**: localhost (para acesso externo)
- **Container names**: `router-db-dev`, `router-api-dev`
- **Volume**: `router-db-data-dev`
- **Network**: `factor_router_net_dev`
- **ENVIRONMENT**: `dev` (variável que seleciona o .env correto)

### Produção (`.env.prod` + `docker-compose-prod.yml`)

- **Router**: `ROUTER_DECISION_MODE=hybrid` (mais estável)
- **LOG_LEVEL**: `warning` (menos ruído)
- **DATABASE_URL**: postgres (interno da rede Docker)
- **Container names**: `router-db-prod`, `router-api-prod`
- **Volume**: `router-db-data-prod`
- **Network**: `factor_router_net_prod`
- **ENVIRONMENT**: `prod` (variável que seleciona o .env correto)

### Como Funciona a Seleção de Ambiente

O gateway usa a variável `ENVIRONMENT` para carregar automaticamente o ficheiro `.env` correto:

- `ENVIRONMENT=dev` → carrega `.env.dev`
- `ENVIRONMENT=prod` → carrega `.env.prod`

**Localmente (desenvolvimento):**
```bash
# Dev
ENVIRONMENT=dev python run.py

# Prod
ENVIRONMENT=prod python run.py
```

**Docker Compose:**
A variável `ENVIRONMENT` já está definida nos ficheiros `docker-compose-*.yml`.

## Comandos

### Ambiente de Desenvolvimento

```bash
# Build + arrancar (background)
docker compose -f docker-compose-dev.yml up -d --build

# Build forçado (recria do zero, sem cache)
docker compose -f docker-compose-dev.yml build --no-cache
docker compose -f docker-compose-dev.yml up -d

# Force recreate (recria containers mesmo sem mudanças)
docker compose -f docker-compose-dev.yml up -d --force-recreate

# Build + force recreate
docker compose -f docker-compose-dev.yml up -d --build --force-recreate

# Ver logs
docker compose -f docker-compose-dev.yml logs -f router-api-dev

# Parar (remove containers, mantém volumes)
docker compose -f docker-compose-dev.yml down

# Parar + remover volumes (APAGA dados da BD!)
docker compose -f docker-compose-dev.yml down -v

# Reiniciar (sem rebuild)
docker compose -f docker-compose-dev.yml restart
```

### Ambiente de Produção

```bash
# Build + arrancar (background)
docker compose -f docker-compose-prod.yml up -d --build

# Build forçado (recria do zero, sem cache)
docker compose -f docker-compose-prod.yml build --no-cache
docker compose -f docker-compose-prod.yml up -d

# Force recreate (recria containers mesmo sem mudanças)
docker compose -f docker-compose-prod.yml up -d --force-recreate

# Build + force recreate
docker compose -f docker-compose-prod.yml up -d --build --force-recreate

# Ver logs
docker compose -f docker-compose-prod.yml logs -f router-api-prod

# Parar (remove containers, mantém volumes)
docker compose -f docker-compose-prod.yml down

# Parar + remover volumes (APAGA dados da BD!)
docker compose -f docker-compose-prod.yml down -v

# Reiniciar (sem rebuild)
docker compose -f docker-compose-prod.yml restart
```

### Ambos os Ambientes Simultaneamente

Os dois ambientes podem correr ao mesmo tempo sem conflitos:

```bash
# Build + arrancar ambos
docker compose -f docker-compose-dev.yml up -d --build
docker compose -f docker-compose-prod.yml up -d --build

# Force recreate ambos
docker compose -f docker-compose-dev.yml up -d --force-recreate
docker compose -f docker-compose-prod.yml up -d --force-recreate

# Ver todos os containers
docker ps | grep router

# Parar ambos
docker compose -f docker-compose-dev.yml down
docker compose -f docker-compose-prod.yml down
```

## Portas

Os ambientes usam **portas diferentes** para poderem correr simultaneamente:

| Serviço | Desenvolvimento | Produção |
|---------|-----------------|----------|
| API | 8003 | 8004 |
| Postgres | 5431 | 3245 |

## Base de Dados

Cada ambiente tem a sua própria base de dados isolada:

- **Dev**: `router-db-data-dev`
- **Prod**: `router-db-data-prod`

As migrações correm automaticamente no primeiro arranque de cada ambiente.

## Verificação

```bash
# Dev
curl http://localhost:8003/health

# Prod
curl http://localhost:8004/health
```
