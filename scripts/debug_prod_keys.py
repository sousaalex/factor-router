#!/usr/bin/env python3
"""
scripts/debug_prod_keys.py

Script de diagnóstico para verificar o estado das apps e API keys no database de produção.

Uso:
    python scripts/debug_prod_keys.py

O script lê as variáveis de ambiente do .env.prod automaticamente.
"""
import asyncio
import hashlib
import os
import sys
from pathlib import Path


def load_env_file(env_path: Path) -> None:
    """Carrega variáveis de ambiente de um arquivo .env sem dependências externas."""
    if not env_path.exists():
        return
    
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            # Não sobrescrever variáveis já definidas no ambiente
            if key not in os.environ:
                os.environ[key] = value


# Carregar .env.prod
env_file = Path(__file__).parent.parent / ".env.prod"
if env_file.exists():
    load_env_file(env_file)
    print(f"✅ .env.prod carregado: {env_file}")
else:
    print(f"⚠️ .env.prod não encontrado em {env_file}")
    print("A usar variáveis de ambiente do sistema...\n")


async def main():
    DATABASE_URL = os.getenv("DATABASE_URL")
    
    if not DATABASE_URL:
        print("❌ ERRO: DATABASE_URL não definida no .env.prod")
        return
    
    print(f"📦 Database: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else 'N/A'}")
    print()
    
    try:
        import asyncpg
    except ImportError:
        print("❌ ERRO: asyncpg não instalado. Instala com: pip install asyncpg")
        return
    
    # Conectar ao database
    print("🔌 A ligar ao database de produção...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("✅ Ligação estabelecida!\n")
    except Exception as e:
        print(f"❌ ERRO ao conectar: {e}")
        return
    
    try:
        # ─────────────────────────────────────────────────────────────────────
        # 1. Listar todas as apps
        # ─────────────────────────────────────────────────────────────────────
        print("=" * 80)
        print("📱 APPS REGISTADAS")
        print("=" * 80)
        
        apps = await conn.fetch("""
            SELECT 
                app_id, 
                name, 
                environment, 
                is_active, 
                spend_cap_usd, 
                spent_usd_total,
                created_at
            FROM gateway_apps
            ORDER BY created_at DESC
        """)
        
        if not apps:
            print("⚠️  Nenhuma app registada neste database.\n")
        else:
            print(f"\nTotal: {len(apps)} app(s)\n")
            for app in apps:
                status_icon = "🟢" if app["is_active"] else "🔴"
                env_icon = "🏭" if app["environment"] == "prod" else "🧪"
                print(f"{status_icon} {env_icon} app_id: {app['app_id']}")
                print(f"    Nome: {app['name']}")
                print(f"    Environment: {app['environment']}")
                print(f"    Ativa: {app['is_active']}")
                print(f"    Gasto: ${app['spent_usd_total']:.4f} / ${app['spend_cap_usd']:.4f}")
                print(f"    Criada: {app['created_at']}")
                print()
        
        # ─────────────────────────────────────────────────────────────────────
        # 2. Listar todas as API keys
        # ─────────────────────────────────────────────────────────────────────
        print("=" * 80)
        print("🔑 API KEYS REGISTADAS")
        print("=" * 80)
        
        keys = await conn.fetch("""
            SELECT 
                k.id,
                k.app_id,
                k.key_prefix,
                k.label,
                k.is_active,
                k.created_at,
                k.revoked_at,
                a.name as app_name,
                a.environment as app_environment,
                a.is_active as app_is_active
            FROM gateway_api_keys k
            LEFT JOIN gateway_apps a ON k.app_id = a.app_id
            ORDER BY k.created_at DESC
        """)
        
        if not keys:
            print("⚠️  Nenhuma API key registada neste database.\n")
        else:
            print(f"\nTotal: {len(keys)} key(s)\n")
            for key in keys:
                status_icon = "🟢" if key["is_active"] else "🔴"
                app_status_icon = "🟢" if key["app_is_active"] else "🔴"
                print(f"{status_icon} Key ID: {key['id']}")
                print(f"    Prefix: {key['key_prefix']}...")
                print(f"    Label: {key['label'] or 'N/A'}")
                print(f"    App: {key['app_id']} ({key['app_name']})")
                print(f"    App Environment: {key['app_environment']} {app_status_icon}")
                print(f"    Key Ativa: {key['is_active']}")
                print(f"    App Ativa: {key['app_is_active']}")
                if key["revoked_at"]:
                    print(f"    ⚠️  Revogada em: {key['revoked_at']}")
                print(f"    Criada: {key['created_at']}")
                print()
        
        # ─────────────────────────────────────────────────────────────────────
        # 3. Instruções para testar uma key específica
        # ─────────────────────────────────────────────────────────────────────
        print("=" * 80)
        print("🔍 TESTAR UMA KEY ESPECÍFICA")
        print("=" * 80)
        print("""
Para verificar se uma API Key específica existe e está ativa:

1. Copia a key completa (ex: sk-fai-e5627b264cf469b5d8dbe06c415dcf74a2f36947c61ce131)

2. Corre este comando no terminal (substitui pela tua key):

   python scripts/debug_prod_keys.py --check-key "sk-fai-..."

Isto vai:
   - Calcular o SHA-256 da key
   - Procurar no database
   - Dizer se está ativa, revogada, ou não existe

""")
        
        # ─────────────────────────────────────────────────────────────────────
        # 4. Resumo de problemas comuns
        # ─────────────────────────────────────────────────────────────────────
        print("=" * 80)
        print("⚠️  PROBLEMAS COMUNS QUE CAUSAM 403")
        print("=" * 80)
        print("""
1. 🔴 App desativada (is_active = false)
   → Solução: UPDATE gateway_apps SET is_active = true WHERE app_id = '...'

2. 🔴 Key revogada (is_active = false em gateway_api_keys)
   → Solução: UPDATE gateway_api_keys SET is_active = true WHERE id = '...'

3. 🔴 App environment mismatch
   → Se a app tem environment='dev' mas estás a usar em produção
   → Solução: UPDATE gateway_apps SET environment = 'prod' WHERE app_id = '...'

4. 🔴 Key não existe neste database
   → A key foi criada em dev mas não em prod
   → Solução: Criar a key em produção via Admin API

""")
        
        # ─────────────────────────────────────────────────────────────────────
        # 5. Comandos SQL úteis
        # ─────────────────────────────────────────────────────────────────────
        print("=" * 80)
        print("🛠️  COMANDOS SQL ÚTEIS")
        print("=" * 80)
        print("""
# Reativar uma app:
UPDATE gateway_apps SET is_active = true WHERE app_id = 'seu-app-id';

# Reativar uma key:
UPDATE gateway_api_keys SET is_active = true WHERE id = 'key-uuid';

# Mudar environment de uma app:
UPDATE gateway_apps SET environment = 'prod' WHERE app_id = 'seu-app-id';

# Verificar uma key específica (substitui o hash):
SELECT * FROM gateway_api_keys WHERE key_hash = 'sha256-hash-aqui';

""")
        
    finally:
        await conn.close()
        print("🔌 Ligação ao database fechada.")


def check_key():
    """Verifica uma key específica via linha de comandos."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Verificar uma API Key específica")
    parser.add_argument("--check-key", type=str, help="API Key completa para verificar")
    args = parser.parse_args()
    
    if not args.check_key:
        print("❌ ERRO: Precisas de fornecer uma key")
        print("Uso: python scripts/debug_prod_keys.py --check-key 'sk-fai-...'")
        return
    
    key = args.check_key.strip()
    print(f"🔍 A verificar key: {key[:20]}...")
    print()
    
    # Calcular hash
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    print(f"🔐 SHA-256 hash: {key_hash}")
    print()
    
    # Procurar no database
    async def lookup():
        DATABASE_URL = os.getenv("DATABASE_URL")
        if not DATABASE_URL:
            print("❌ ERRO: DATABASE_URL não definida")
            return
        
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        
        try:
            row = await conn.fetchrow("""
                SELECT 
                    k.id,
                    k.app_id,
                    k.key_prefix,
                    k.label,
                    k.is_active,
                    k.created_at,
                    k.revoked_at,
                    a.name as app_name,
                    a.environment as app_environment,
                    a.is_active as app_is_active,
                    a.spend_cap_usd,
                    a.spent_usd_total
                FROM gateway_api_keys k
                LEFT JOIN gateway_apps a ON k.app_id = a.app_id
                WHERE k.key_hash = $1
            """, key_hash)
            
            if row is None:
                print("❌ KEY NÃO ENCONTRADA")
                print()
                print("Esta key não existe no database de produção.")
                print("Possíveis causas:")
                print("  - A key foi criada apenas em desenvolvimento")
                print("  - A key foi apagada de produção")
                print()
                print("Solução: Criar uma nova key em produção via Admin API")
                print("  POST /admin/apps/{app_id}/keys")
                return
            
            print("✅ KEY ENCONTRADA!")
            print()
            print(f"Key ID: {row['id']}")
            print(f"App ID: {row['app_id']}")
            print(f"App Nome: {row['app_name']}")
            print(f"Label: {row['label'] or 'N/A'}")
            print()
            print("📊 ESTADO:")
            print(f"  Key Ativa: {'🟢 SIM' if row['is_active'] else '🔴 NÃO'}")
            print(f"  App Ativa: {'🟢 SIM' if row['app_is_active'] else '🔴 NÃO'}")
            print(f"  App Environment: {row['app_environment']}")
            print()
            
            if row["revoked_at"]:
                print(f"⚠️  KEY REVOGADA em: {row['revoked_at']}")
                print()
            
            print("💰 GASTO DA APP:")
            print(f"  Gasto: ${row['spent_usd_total']:.4f} / ${row['spend_cap_usd']:.4f}")
            print(f"  Restante: ${max(0, row['spend_cap_usd'] - row['spent_usd_total']):.4f}")
            print()
            
            # Diagnóstico
            print("🔍 DIAGNÓSTICO:")
            if not row['is_active']:
                print("  ❌ PROBLEMA: A key está inativa/revogada")
                print("  → Solução: UPDATE gateway_api_keys SET is_active = true WHERE id = '{}';".format(row['id']))
            elif not row['app_is_active']:
                print("  ❌ PROBLEMA: A app está desativada")
                print("  → Solução: UPDATE gateway_apps SET is_active = true WHERE app_id = '{}';".format(row['app_id']))
            elif row['app_environment'] != 'prod':
                print(f"  ⚠️  ATENÇÃO: A app tem environment='{row['app_environment']}' (não é 'prod')")
                print("  → Solução: UPDATE gateway_apps SET environment = 'prod' WHERE app_id = '{}';".format(row['app_id']))
            else:
                print("  ✅ Tudo parece correto no database!")
                print("  Se ainda recebes 403, o problema pode ser:")
                print("    - Cache desatualizado (reinicia o router-api-prod)")
                print("    - Key errada a ser usada no cliente")
                print("    - Problema de rede/proxy entre o cliente e o router")
            
        finally:
            await conn.close()
    
    asyncio.run(lookup())


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--check-key":
        check_key()
    else:
        asyncio.run(main())
