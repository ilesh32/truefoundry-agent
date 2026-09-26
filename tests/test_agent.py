"""Offline tests: the agent spec, and the approval/pause handling in chat.py."""
import io
from types import SimpleNamespace as NS

from janitor_agent import chat, config, spec


def test_spec_gates_teardown_by_name_and_attaches_the_mcp_server():
    server = spec.manifest()["mcp_servers"][0]
    assert server["name"] == config.MCP_SERVER_NAME
    assert server["require_approval_for_tools"] == ["execute_teardown"]  # not the annotation-based @destructive
    assert spec.manifest()["model"]["name"] == config.MODEL


def test_instructions_pin_the_region_and_forbid_invented_ids():
    text = spec.instructions()
    assert config.AWS_REGION in text
    assert "Never invent" in text


def _pending_setup():
    message = NS(id="m1", type="model.message",
                 tool_calls=[NS(id="c1", function=NS(name="execute_teardown", arguments='{"resource_ids": ["vol-1"]}'))])
    pending = NS(thread_id="main", tool_calls=[NS(id="c1", source_event_id="m1")])
    return {"m1": message}, [pending]


def test_resolve_pending_looks_up_tool_name_and_arguments():
    events, pending = _pending_setup()
    [call] = chat.resolve_pending(pending, events)
    assert (call.name, call.tool_call_id, call.thread_id) == ("execute_teardown", "c1", "main")
    assert "vol-1" in call.arguments


def test_resolve_pending_skips_calls_it_cannot_find():
    _, pending = _pending_setup()
    assert chat.resolve_pending(pending, {}) == []


def _call():
    return chat.PendingCall("main", "c1", "execute_teardown", '{"resource_ids": ["vol-1"]}')


def test_approval_defaults_to_deny():
    for answer in ("", "n", "no", "maybe"):
        item = chat.ask_approval(_call(), input_fn=lambda _: answer, out=io.StringIO())
        assert item["approval"]["status"] == "deny"


def test_approval_allows_only_on_explicit_yes():
    for answer in ("y", "YES", " y "):
        item = chat.ask_approval(_call(), input_fn=lambda _: answer, out=io.StringIO())
        assert item["approval"] == {"status": "allow"}
        assert (item["type"], item["tool_call_id"]) == ("user.tool_approval", "c1")


def test_approval_prompt_shows_the_arguments_and_warns_about_deletion():
    out = io.StringIO()
    chat.ask_approval(_call(), input_fn=lambda _: "n", out=out)
    assert "vol-1" in out.getvalue() and "PERMANENTLY" in out.getvalue()


def test_converse_resumes_the_turn_with_the_users_decision(monkeypatch):
    turns = []

    def fake_stream_turn(client, session_id, items, out):
        turns.append(items)
        if len(turns) == 1:
            return chat.TurnResult(status="done", approvals=[_call()])
        return chat.TurnResult(status="done")

    monkeypatch.setattr(chat, "stream_turn", fake_stream_turn)
    chat.converse(None, "s1", "delete vol-1", input_fn=lambda _: "n", out=io.StringIO())

    assert turns[0] == [{"type": "user.message", "content": "delete vol-1"}]
    assert turns[1][0]["type"] == "user.tool_approval" and turns[1][0]["approval"]["status"] == "deny"
    assert len(turns) == 2  # stops once nothing is pending


def test_instructions_forbid_answering_from_earlier_results():
    text = spec.instructions()
    assert "STALE" in text and "call the appropriate tool again" in text
    assert "re-run analyze_infrastructure" in text  # fresh scan right before any teardown


def test_approval_is_denied_when_there_is_no_terminal_to_ask():
    def no_input(_prompt):
        raise EOFError

    item = chat.ask_approval(_call(), input_fn=no_input, out=io.StringIO())
    assert item["approval"]["status"] == "deny"


def test_mcp_token_can_come_from_a_mounted_secret_file(tmp_path, monkeypatch):
    import importlib

    secret = tmp_path / "mcp_token"
    secret.write_text("file-token-value-123\n")
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MCP_AUTH_TOKEN_FILE", str(secret))
    try:
        assert importlib.reload(config).MCP_AUTH_TOKEN == "file-token-value-123"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
