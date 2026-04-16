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

if not Path(env_file).exists():
    raise FileNotFoundError(
        f"Environment file '{env_file}' not found. "
        f"Set ENVIRONMENT=dev or ENVIRONMENT=prod, or create the file."
    )

# Load environment variables from selected .env file
load_dotenv(env_file)
print(f"✅ Loaded environment: {env_file}")
PORT = int(os.getenv("PORT", 8003))
HOST = os.getenv("HOST", "0.0.0.0")
if __name__ == "__main__":
    uvicorn.run(
        "src.api.app:app",
        host=HOST,
        port=PORT,
        reload=True,  # Hot reload during development
    )
