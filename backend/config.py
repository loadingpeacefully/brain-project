"""Brain configuration — reads from .env file."""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BRAIN_FILE = os.environ.get("BRAIN_FILE", "data/brain.json")
MODEL_EXTRACTION = os.environ.get("MODEL_EXTRACTION", "claude-sonnet-4-6")
MODEL_FAST = os.environ.get("MODEL_FAST", "claude-haiku-4-5-20251001")
MAX_DOC_CHARS = int(os.environ.get("MAX_DOC_CHARS", "18000"))
PORT = int(os.environ.get("PORT", "8000"))
