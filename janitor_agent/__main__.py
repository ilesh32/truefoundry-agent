"""Single entrypoint, used by the Docker image:

    python -m janitor_agent register        # register MCP server + agent in TrueForge (idempotent)
    python -m janitor_agent check           # smoke test: TrueForge up, agent registered, MCP tools reachable
    python -m janitor_agent chat [--fresh] [message...]
"""
import sys

USAGE = __doc__


def check() -> int:
    from janitor_agent import config

    client = config.make_client()
    agents = [a for a in client.agents.list(agent_name=config.AGENT_NAME) if a.name == config.AGENT_NAME]
    if not agents:
        print(f"FAIL agent {config.AGENT_NAME!r} is not registered; run: register")
        return 1
    print(f"OK   TrueForge reachable at {config.BASE_URL}; agent {config.AGENT_NAME!r} registered")

    tools = {t["name"] for t in client.mcp_servers.list_tools(name=config.MCP_SERVER_NAME).data}
    missing = {"list_resources", "analyze_infrastructure", "execute_teardown"} - tools
    if missing:
        print(f"FAIL MCP server is missing tools: {sorted(missing)}")
        return 1
    print(f"OK   MCP server {config.MCP_SERVER_NAME!r} reachable by TrueForge; tools: {sorted(tools)}")
    if config.USE_SANDBOX:
        sandbox_tools = {t["name"] for t in client.mcp_servers.list_tools(name=config.SANDBOX_MCP_NAME).data}
        if "run_python" not in sandbox_tools:
            print(f"FAIL sandbox MCP server {config.SANDBOX_MCP_NAME!r} has no run_python tool")
            return 1
        print(f"OK   sandbox MCP server {config.SANDBOX_MCP_NAME!r} reachable by TrueForge; tools: {sorted(sandbox_tools)}")
    return 0


def main() -> int:
    command, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if command == "register":
        from janitor_agent import register

        register.main()
    elif command == "check":
        return check()
    elif command == "chat":
        from janitor_agent import chat

        sys.argv = [sys.argv[0], *rest]
        chat.main()
    else:
        print(USAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
