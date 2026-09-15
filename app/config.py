"""Central configuration. Everything is read from .env (or the environment)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)   # .env is the source of truth, even over shell variables


_PLACEHOLDERS = ("<", "change-me", "sk-ant-...", "...")


def _env(name: str, default: str | None = None) -> str | None:
    """Read a setting; untouched .env.example placeholders count as unset."""
    value = (os.getenv(name) or "").strip()
    if value.startswith("#"):                 # dotenv keeps "KEY=   # comment" as the value
        value = ""
    elif " #" in value:
        value = value.split(" #", 1)[0].strip()
    if not value or any(p in value for p in _PLACEHOLDERS):
        return default
    return value


# --- Claude ---------------------------------------------------------------
# Which model service powers the brain:
#   anthropic = Claude via console.anthropic.com credits (best quality)
#   vertex    = Claude via Google Cloud Vertex AI (billed to the GCP project)
#   gemini    = Google Gemini Developer API (free tier; no billing needed)
LLM_PROVIDER = (_env("LLM_PROVIDER") or _env("CLAUDE_PROVIDER") or "anthropic").lower()
CLAUDE_PROVIDER = LLM_PROVIDER
VERTEX_PROJECT_ID = _env("VERTEX_PROJECT_ID")
VERTEX_REGION = _env("VERTEX_REGION", "global")
GEMINI_API_KEY = _env("GEMINI_API_KEY")                 # optional; otherwise the service account is used
GEMINI_PROJECT_ID = _env("GEMINI_PROJECT_ID") or VERTEX_PROJECT_ID
GEMINI_MODEL = _env("GEMINI_MODEL", "auto")             # auto = newest flash model available
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-opus-5")
ANTHROPIC_EFFORT = _env("ANTHROPIC_EFFORT", "high")          # low | medium | high | xhigh | max
ANTHROPIC_MAX_TOKENS = int(_env("ANTHROPIC_MAX_TOKENS", "32000"))
ANTHROPIC_FALLBACKS = _env("ANTHROPIC_FALLBACKS", "1") == "1"  # server-side refusal fallbacks

# --- Admissions portal MCP server (PUAP) ------------------------------------
PUAP_MCP_URL = _env("PUAP_MCP_URL")            # streamable-HTTP endpoint of the PUAP MCP server
PUAP_MCP_TOKEN = _env("PUAP_MCP_TOKEN")        # optional bearer token for that endpoint
PUAP_MCP_COMMAND = _env("PUAP_MCP_COMMAND")    # alternative: local stdio command, e.g. "python /opt/puap-mcp/server.py"
TOOL_RESULT_MAX_CHARS = int(_env("TOOL_RESULT_MAX_CHARS", "80000"))
TOOL_CALL_TIMEOUT = float(_env("TOOL_CALL_TIMEOUT", "300"))
MAX_TOOL_ROUNDS = int(_env("MAX_TOOL_ROUNDS", "25"))
EXTRA_DENIED_TOOLS = {t.strip() for t in (_env("EXTRA_DENIED_TOOLS", "") or "").split(",") if t.strip()}

# --- Telegram (long polling - no public URL needed) -------------------------
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")

# --- WhatsApp Cloud API (webhook - needs a public HTTPS URL) -----------------
WHATSAPP_TOKEN = _env("WHATSAPP_TOKEN")                      # permanent system-user access token
WHATSAPP_PHONE_NUMBER_ID = _env("WHATSAPP_PHONE_NUMBER_ID")  # from WhatsApp > API Setup in the Meta app
WHATSAPP_VERIFY_TOKEN = _env("WHATSAPP_VERIFY_TOKEN")        # any string; paste the same one in the webhook config
WHATSAPP_APP_SECRET = _env("WHATSAPP_APP_SECRET")            # Meta app secret, used to verify webhook signatures
WHATSAPP_API_VERSION = _env("WHATSAPP_API_VERSION", "v21.0")
WEBHOOK_HOST = _env("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(_env("WEBHOOK_PORT", "8765"))

# --- Google Sheets reference data (read-only) --------------------------------------
DATA_DIR = ROOT / "data"
GOOGLE_SERVICE_ACCOUNT_FILE = _env("GOOGLE_SERVICE_ACCOUNT_FILE", str(DATA_DIR / "google-service-account.json"))
REFERENCE_SHEET_ID = _env("REFERENCE_SHEET_ID", "1mgmv4Cc67olcCN-U7vkNC0q-MVD-23F0_UCAdRODyvk")
REFERENCE_SHEET_TABS = [t.strip() for t in (_env("REFERENCE_SHEET_TABS",
                        "Board Exam & Result Dates,Entrance Exam & Result Dates,Last Dates Performance") or "").split(",") if t.strip()]
# When the primary brain's quota is exhausted, retry the same question on this provider (anthropic | vertex | none)
LLM_FALLBACK_PROVIDER = (_env("LLM_FALLBACK_PROVIDER", "anthropic") or "none").lower()
REFERENCE_REFRESH_HOURS = float(_env("REFERENCE_REFRESH_HOURS", "6"))

# --- Paths --------------------------------------------------------------------
USERS_FILE = DATA_DIR / "users.json"
MEMORY_DIR = DATA_DIR / "memory"
