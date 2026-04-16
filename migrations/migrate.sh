#!/usr/bin/env bash
# Uso (a partir de qualquer pasta):
#   ./migrations/migrate.sh 005_openrouter_credits_state.sql
#   ./migrations/migrate.sh 005              # único ficheiro que começa por 005_
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Permite sobrescrever de fora:
#   MIGRATE_ENV_FILE=/caminho/para/arquivo ./migrations/migrate.sh 005
ENV_FILE="${MIGRATE_ENV_FILE:-}"
AUTO_YES="${MIGRATE_YES:-}"

resolve_env_file() {
  local selector="${ENVIRONMENT:-${ENVOROMENT:-${ENV:-}}}"
  selector="$(echo "$selector" | tr '[:upper:]' '[:lower:]' | xargs)"

  # Preferir `.env.dev`/`.env.prod` quando existirem.
  local candidate_dev="${ROOT_DIR}/.env.dev"
  local candidate_prod="${ROOT_DIR}/.env.prod"
  local candidate_base="${ROOT_DIR}/.env"

  local candidates=()
  [[ -f "$candidate_dev" ]] && candidates+=("dev|$candidate_dev")
  [[ -f "$candidate_prod" ]] && candidates+=("prod|$candidate_prod")
  [[ -f "$candidate_base" ]] && candidates+=("base|$candidate_base")

  # Se foi definido qual ambiente escolher, respeitar.
  if [[ "$selector" == "dev" || "$selector" == "prod" || "$selector" == "base" || "$selector" == "env" ]]; then
    local picked=""
    if [[ "$selector" == "env" ]]; then selector="base"; fi
    if [[ "$selector" == "dev" ]]; then picked="$candidate_dev"; fi
    if [[ "$selector" == "prod" ]]; then picked="$candidate_prod"; fi
    if [[ "$selector" == "base" ]]; then picked="$candidate_base"; fi
    if [[ -f "$picked" ]]; then
      echo "$picked"
      return 0
    fi
    # se não existir, cai no modo interativo abaixo (caso haja múltiplos)
  fi

  # Zero ou 1 candidato -> escolher direto
  if [[ ${#candidates[@]} -eq 0 ]]; then
    return 1
  fi
  if [[ ${#candidates[@]} -eq 1 ]]; then
    echo "${candidates[0]#*|}"
    return 0
  fi

  # Múltiplos candidatos: escolher via prompt se estiver em TTY
  if [[ -t 0 && -z "${AUTO_YES:-}" ]]; then
    echo "Foram encontrados múltiplos ficheiros .env:" >&2
    local i=1
    local c
    for c in "${candidates[@]}"; do
      local tag="${c%%|*}"
      local p="${c#*|}"
      echo "  ${i}) ${p}  (${tag})" >&2
      ((i+=1)) || true
    done

    local default_idx=1
    if [[ -f "$candidate_prod" && -n "$selector" && "$selector" == "prod" ]]; then
      default_idx=2
    elif [[ -f "$candidate_prod" && -n "$selector" && "$selector" == "dev" ]]; then
      default_idx=1
    fi

    read -r -p "Qual usar? [${default_idx}]: " choice || true
    choice="${choice:-$default_idx}"
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#candidates[@]} )); then
      echo "${candidates[$((choice-1))]#*|}"
      return 0
    fi
  fi

  # Sem prompt (CI/não-tty) ou escolha inválida: preferir dev se existir, senão base, senão o primeiro.
  if [[ -f "$candidate_dev" ]]; then
    echo "$candidate_dev"
    return 0
  fi
  if [[ -f "$candidate_base" ]]; then
    echo "$candidate_base"
    return 0
  fi

  echo "${candidates[0]#*|}"
  return 0
}

usage() {
  echo "Uso: $0 <ficheiro.sql | prefixo, ex. 005>" >&2
  echo "  Se NÃO passar argumento, tenta executar TODAS as migrações (sequencialmente)." >&2
  echo "  Lê DATABASE_URL do ambiente (se já existir) ou de um .env (dev/prod)." >&2
  echo "  Opções: MIGRATE_YES=1 (não pergunta). ENVIRONMENT=dev|prod para selecionar env." >&2
  exit 1
}

if [[ $# -ge 1 ]]; then
  arg="$1"
else
  arg=""
fi

confirm_run_all() {
  # Só perguntar em TTY e quando não foi dado MIGRATE_YES=1
  if [[ -t 0 && -z "${AUTO_YES:-}" ]]; then
    read -r -p "Executar TODAS as migrações agora? (y/N): " yn || true
    yn="${yn:-N}"
    [[ "$yn" == "y" || "$yn" == "Y" ]]
    return $?
  fi
  return 0
}

resolve_sql_path() {
  local a="$1"
  if [[ -f "$a" ]]; then
    echo "$(cd "$(dirname "$a")" && pwd)/$(basename "$a")"
    return 0
  fi
  if [[ -f "$SCRIPT_DIR/$a" ]]; then
    echo "$SCRIPT_DIR/$a"
    return 0
  fi
  if [[ "$a" == *.sql ]]; then
    echo "Ficheiro não encontrado: $a (nem em $SCRIPT_DIR)" >&2
    return 1
  fi
  local matches
  matches=( "$SCRIPT_DIR"/"${a}"_*.sql )
  if [[ ${#matches[@]} -eq 1 && -f "${matches[0]}" ]]; then
    echo "${matches[0]}"
    return 0
  fi
  if [[ ${#matches[@]} -eq 0 ]]; then
    echo "Nenhuma migração com prefixo '${a}_' em $SCRIPT_DIR" >&2
    return 1
  fi
  echo "Ambíguo: vários ficheiros para prefixo '$a':" >&2
  printf '  %s\n' "${matches[@]}" >&2
  return 1
}

run_migration_file() {
  local sql_path="$1"
  local database_url="$2"
  echo "→ Migração: $sql_path"
  psql "$database_url" -v ON_ERROR_STOP=1 -f "$sql_path"
  echo "→ OK"
}

read_database_url() {
  # Se o DATABASE_URL já foi injetado (ex.: via docker-compose env_file),
  # não precisamos de ler o ficheiro.
  if [[ -n "${DATABASE_URL:-}" ]]; then
    printf '%s' "$DATABASE_URL"
    return 0
  fi

  # Caso não exista no ambiente, tenta carregar de um .env.
  if [[ -z "$ENV_FILE" ]]; then
    ENV_FILE="$(resolve_env_file)" || {
      echo "Não encontrei nenhum .env para carregar DATABASE_URL (procurei dev/prod e .env)." >&2
      return 1
    }
  fi

  [[ -f "$ENV_FILE" ]] || { echo "Falta ${ENV_FILE}" >&2; return 1; }
  local line val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//$'\r'/}"
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" == DATABASE_URL=* ]]; then
      val="${line#DATABASE_URL=}"
      val="${val#\"}"; val="${val%\"}"
      val="${val#\'}"; val="${val%\'}"
      val="${val#"${val%%[![:space:]]*}"}"
      val="${val%"${val##*[![:space:]]}"}"
      if [[ -n "$val" ]]; then
        printf '%s' "$val"
        return 0
      fi
    fi
  done < "$ENV_FILE"
  echo "DATABASE_URL não definido em ${ENV_FILE}" >&2
  return 1
}

if ! command -v psql >/dev/null 2>&1; then
  echo "Comando 'psql' não encontrado (instala cliente PostgreSQL)." >&2
  exit 1
fi

sql_paths=()
if [[ -z "${arg}" ]]; then
  confirm_run_all || exit 0

  sql_paths=( "$SCRIPT_DIR"/[0-9][0-9][0-9]_*\.sql )
  # Se glob não expandiu, mantém literal no array
  if [[ ${#sql_paths[@]} -eq 1 && "${sql_paths[0]}" == "$SCRIPT_DIR"/[0-9][0-9][0-9]_*\.sql ]]; then
    echo "Nenhuma migração encontrada em $SCRIPT_DIR." >&2
    exit 1
  fi

  # Ordenar para garantir execução 001 -> 007
  mapfile -t sql_paths < <(printf '%s\n' "${sql_paths[@]}" | sort)
else
  SQL_PATH="$(resolve_sql_path "$arg")" || exit 1
  sql_paths=( "$SQL_PATH" )
fi

DATABASE_URL="$(read_database_url)" || exit 1
DATABASE_URL="${DATABASE_URL/postgresql+asyncpg:\/\//postgresql:\/\/}"

for SQL_PATH in "${sql_paths[@]}"; do
  run_migration_file "$SQL_PATH" "$DATABASE_URL"
done

echo "→ OK"
