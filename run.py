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
if env_path.exists():
    # Local execution convenience (e.g. `ENVIRONMENT=dev python run.py`)
    load_dotenv(env_path)
    print(f"✅ Loaded environment file: {env_path}")
else:
    # In Docker, `env_file:` injects vars but does not mount the file into the container.
    print(
        f"ℹ️ Environment file not found: {env_path} "
        "(continuing with process environment variables)"
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
