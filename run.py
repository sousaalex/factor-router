"""
run.py - Main file to start the FastAPI API
Execute: uv run run.py

Environment selection:
  ENVIRONMENT=dev uv run run.py   # development
  ENVIRONMENT=prod uv run run.py  # production
"""
import os
from pathlib import Path
import uvicorn
from dotenv import load_dotenv

# Resolve environment file based on ENVIRONMENT variable
env_selector = os.getenv("ENVIRONMENT", "dev").strip().lower()
env_file = f".env.{env_selector}"

env_path = Path(env_file)
in_docker = Path("/.dockerenv").exists()
if env_path.exists():
    # Local execution convenience (e.g. `ENVIRONMENT=dev python run.py`)
    load_dotenv(env_path)
    print(f"✅ Loaded environment file: {env_path}")
else:
    # Docker Compose `env_file:` injeta as variáveis no ambiente do processo,
    # mas normalmente NÃO monta o ficheiro dentro do container.
    # Por isso, dentro do Docker, este aviso tende a ser só ruído.
    if not in_docker:
        print(
            f"⚠️ Environment file not found: {env_path}. "
            "Defina as variáveis no ambiente ou garanta que `.env.dev`/`.env.prod` "
            "está no diretório de execução."
        )
PORT = int(os.getenv("PORT", 8003))
HOST = os.getenv("HOST", "0.0.0.0")
if __name__ == "__main__":
    uvicorn.run(
        "src.api.app:app",
        host=HOST,
        port=PORT,
        reload=(env_selector == "dev"),
    )
