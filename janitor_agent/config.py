"""Settings, read from the environment / .env, with defaults that follow the
sibling ../mcp-s2sep project so a fresh checkout works without configuration."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
MCP_PROJECT = ROOT.parent / "mcp-s2sep"

load_dotenv(ROOT / ".env")

AGENT_NAME = "cloud-cost-janitor"
MCP_SERVER_NAME = "cloud-cost-janitor-mcp"
SANDBOX_MCP_NAME = "cost-sandbox-mcp"


def _from_mcp_project(filename: str, key: str | None = None) -> str | None:
    path = MCP_PROJECT / filename
    if not path.is_file():
        return None
    text = path.read_text().strip()
    if key is None:
        return text or None
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip() or None
    return None


# The compose file maps the container's 8790 to host port 8791.
BASE_URL = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8791")
TOKEN = os.environ.get("TRUEFORGE_TOKEN") or None
MODEL = os.environ.get("AGENT_MODEL", "test/vm-polaris-openai")

# Compute costs by having the agent write and run a script in the LOCAL Docker sandbox (the
# sibling ../sandbox-mcp server; TrueForge's own cloud sandbox stays off). Off: the agent only
# quotes the MCP server's numbers.
USE_SANDBOX = os.environ.get("USE_SANDBOX", "true").lower() in ("1", "true", "yes")

# Strict freshness: open a brand-new session for every message, so the model has no earlier
# tool output in its context and MUST call the MCP server. Trade-off: no follow-ups across messages.
FRESH_SESSION_PER_MESSAGE = os.environ.get("FRESH_SESSION_PER_MESSAGE", "").lower() in ("1", "true", "yes")

# Resolved by the TrueForge *server*, which runs in Docker: localhost would be the container.
MCP_URL = os.environ.get("MCP_URL", "http://host.docker.internal:8000/mcp")


def _read_file(path: str | None) -> str | None:
    return Path(path).read_text().strip() or None if path and Path(path).is_file() else None


# Precedence: env var, then a mounted secret file (containers), then the sibling project's token.
MCP_AUTH_TOKEN = (
    os.environ.get("MCP_AUTH_TOKEN")
    or _read_file(os.environ.get("MCP_AUTH_TOKEN_FILE"))
    or _from_mcp_project(".mcp_token")
)
SANDBOX_MCP_URL = os.environ.get("SANDBOX_MCP_URL", "http://host.docker.internal:8100/mcp")
SANDBOX_MCP_TOKEN = (
    os.environ.get("SANDBOX_MCP_TOKEN")
    or _read_file(os.environ.get("SANDBOX_MCP_TOKEN_FILE"))
    or _read_file(str(ROOT.parent / "sandbox-mcp" / ".token"))
)
AWS_REGION = os.environ.get("AWS_REGION") or _from_mcp_project(".env", "AWS_REGION") or "us-east-1"


def make_client():
    from trueforge_sdk import TrueForge

    return TrueForge(base_url=BASE_URL, token=TOKEN, timeout=600)
