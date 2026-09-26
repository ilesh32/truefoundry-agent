"""Idempotently register the MCP server and the agent in the local TrueForge.

    python -m janitor_agent.register

Safe to re-run: the connector is create-or-update, and the agent is updated in place.
"""
import sys

from trueforge_sdk import McpServerHeaderAuth, RemoteMcpServerManifest

from janitor_agent import config, spec


def register_mcp_server(client) -> None:
    if not config.MCP_AUTH_TOKEN:
        sys.exit("No MCP auth token: set MCP_AUTH_TOKEN or run mcp-s2sep/scripts/deploy_local.sh up")
    client.settings.mcp_servers.create_or_update(
        manifest=RemoteMcpServerManifest(
            name=config.MCP_SERVER_NAME,
            url=config.MCP_URL,
            description="AWS inventory, idle-resource scan and gated teardown (Cloud Cost Janitor).",
            auth=McpServerHeaderAuth(headers={"Authorization": f"Bearer {config.MCP_AUTH_TOKEN}"}),
        )
    )


def register_sandbox_server(client) -> None:
    """Attach the local Docker sandbox (../sandbox-mcp) as a second MCP server."""
    if not config.SANDBOX_MCP_TOKEN:
        sys.exit("No sandbox token: run sandbox-mcp/scripts/run.sh start, set SANDBOX_MCP_TOKEN, or set USE_SANDBOX=false")
    client.settings.mcp_servers.create_or_update(
        manifest=RemoteMcpServerManifest(
            name=config.SANDBOX_MCP_NAME,
            url=config.SANDBOX_MCP_URL,
            description="Disposable local Docker sandbox that runs Python (stdlib only, no network) for calculations.",
            auth=McpServerHeaderAuth(headers={"Authorization": f"Bearer {config.SANDBOX_MCP_TOKEN}"}),
        )
    )
    tools = [t["name"] for t in client.mcp_servers.list_tools(name=config.SANDBOX_MCP_NAME).data]
    if "run_python" not in tools:
        sys.exit(f"sandbox MCP server is registered but has no run_python tool (found {tools})")
    print(f"registered sandbox MCP server {config.SANDBOX_MCP_NAME!r} -> {config.SANDBOX_MCP_URL}; tools: {tools}")


def register_agent(client) -> str:
    existing = next((a for a in client.agents.list(agent_name=config.AGENT_NAME) if a.name == config.AGENT_NAME), None)
    if existing is not None:
        client.agents.update(agent_id=existing.id, description=spec.DESCRIPTION, manifest=spec.manifest())
        return existing.id
    return client.agents.create(
        name=config.AGENT_NAME, description=spec.DESCRIPTION, manifest=spec.manifest()
    ).data.id


def main() -> None:
    client = config.make_client()
    register_mcp_server(client)
    print(f"registered MCP server {config.MCP_SERVER_NAME!r} -> {config.MCP_URL}")

    # Prove TrueForge (inside Docker) can actually reach and authenticate to the MCP server.
    tools = client.mcp_servers.list_tools(name=config.MCP_SERVER_NAME)
    names = [t["name"] for t in tools.data]
    print(f"TrueForge discovered tools: {names}")
    missing = {"list_resources", "analyze_infrastructure", "execute_teardown"} - set(names)
    if missing:
        sys.exit(f"MCP server is registered but these tools are missing: {sorted(missing)}")

    if config.USE_SANDBOX:
        register_sandbox_server(client)
    agent_id = register_agent(client)
    print(f"registered agent {config.AGENT_NAME!r} (id {agent_id}), model {config.MODEL}")
    print(f"approval required for: {spec.APPROVAL_REQUIRED_TOOLS}")


if __name__ == "__main__":
    main()
