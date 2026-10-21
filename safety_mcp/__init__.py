"""safety-mcp-server: безопасный MCP-доступ к стендам по SSH."""
import os

__version__ = "0.1.0"
SERVER_NAME = "safety-mcp-server"


def state_dir() -> str:
    return os.environ.get("MCP_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".local", "state", SERVER_NAME)


def queue_dir() -> str:
    return os.environ.get("MCP_QUEUE_DIR") or os.path.join(state_dir(), "queue")
