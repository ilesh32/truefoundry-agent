"""Idempotently register the MCP server and the agent in the local TrueForge.

    python -m janitor_agent.register

Safe to re-run: the connector is create-or-update, and the agent is updated in place.
"""
import sys

from trueforge_sdk import McpServerHeaderAuth, RemoteMcpServerManifest

from janitor_agent import config, scheduled_spec, spec


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


def find_agent(client, name: str):
    return next((a for a in client.agents.list(agent_name=name) if a.name == name), None)


def register_agent(client, name: str = config.AGENT_NAME, description: str = spec.DESCRIPTION, manifest: dict | None = None) -> str:
    manifest = manifest if manifest is not None else spec.manifest()
    existing = find_agent(client, name)
    if existing is not None:
        client.agents.update(agent_id=existing.id, description=description, manifest=manifest)
        return existing.id
    return client.agents.create(name=name, description=description, manifest=manifest).data.id


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

    agent_id = register_agent(client)
    print(f"registered agent {config.AGENT_NAME!r} (id {agent_id}), model {config.MODEL}")
    scheduled_id = register_agent(
        client, config.SCHEDULED_AGENT_NAME, scheduled_spec.DESCRIPTION, scheduled_spec.manifest()
    )
    print(f"registered agent {config.SCHEDULED_AGENT_NAME!r} (id {scheduled_id}) for scheduled runs")
    print(f"approval required for: {spec.APPROVAL_REQUIRED_TOOLS}")


if __name__ == "__main__":
    main()
