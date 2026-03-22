"""
run.py - Main file to start the FastAPI API
Execute: uv run run.py
"""
import os
import uvicorn
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
PORT = int(os.getenv("PORT", 8003))
HOST = os.getenv("HOST", "0.0.0.0")
if __name__ == "__main__":
    uvicorn.run(
        "src.api.app:app",
        host=HOST,
        port=PORT,
        reload=True,  # Hot reload during development
    )
