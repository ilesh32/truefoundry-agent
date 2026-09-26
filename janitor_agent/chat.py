"""Interactive chat with the Cloud Cost Janitor agent running in local TrueForge.

    python -m janitor_agent.chat                      # REPL
    python -m janitor_agent.chat "what do I have?"    # one message, then exit
    python -m janitor_agent.chat --fresh              # new session per message: never reuses earlier results

Streams the reply live, shows tool calls and results, and pauses for you to
approve or deny every gated tool call (execute_teardown deletes for real).
"""
import json
import sys
from dataclasses import dataclass, field

from trueforge_sdk.events import is_event_delta, merge_event_delta

from janitor_agent import config

RESULT_PREVIEW_CHARS = 1500


@dataclass
class PendingCall:
    thread_id: str
    tool_call_id: str
    name: str
    arguments: str


@dataclass
class TurnResult:
    status: str = "unknown"
    error: str | None = None
    approvals: list[PendingCall] = field(default_factory=list)
    questions: list[PendingCall] = field(default_factory=list)


def resolve_pending(pending_events, events) -> list[PendingCall]:
    """Turn tool.approval_required / tool.response_required events into calls with
    their tool name and arguments, looked up from the model.message that made them."""
    calls = []
    for pending in pending_events:
        for ref in pending.tool_calls:
            message = events.get(ref.source_event_id)
            call = next((tc for tc in (getattr(message, "tool_calls", None) or []) if tc.id == ref.id), None)
            if call is None:
                continue
            calls.append(PendingCall(pending.thread_id, ref.id, call.function.name, call.function.arguments or ""))
    return calls


def stream_turn(client, session_id: str, items: list[dict], out=sys.stdout) -> TurnResult:
    events: dict = {}
    approvals, questions = [], []
    result = TurnResult()
    streamed_text = False

    for event in client.sessions.create_turn_stream(session_id=session_id, input=items):
        if is_event_delta(event):
            base = events.get(event.id)
            if base is not None:
                merge_event_delta(base, event)
            if event.type == "model.message.delta" and event.thread_id == "main" and event.content:
                print(event.content, end="", flush=True, file=out)
                streamed_text = True
            continue

        events[event.id] = event
        if event.type == "model.message":
            for call in event.tool_calls or []:
                print(f"\n  → {call.function.name}({call.function.arguments})", file=out)
        elif event.type == "tool.response":
            preview = event.content if len(event.content) <= RESULT_PREVIEW_CHARS else event.content[:RESULT_PREVIEW_CHARS] + " …"
            print(f"  ← {preview}", file=out)
        elif event.type == "tool.approval_required":
            approvals.append(event)
        elif event.type == "tool.response_required":
            questions.append(event)
        elif event.type == "mcp.auth_required":
            print(f"\n  ! MCP server needs authorization: {event}", file=out)
        elif event.type == "turn.done":
            result.status = event.state.status
            result.error = getattr(event.state, "message", None) if result.status == "error" else None

    if streamed_text:
        print(file=out)
    result.approvals = resolve_pending(approvals, events)
    result.questions = [c for c in resolve_pending(questions, events) if c.name == "ask_user_question"]
    return result


def ask_approval(call: PendingCall, input_fn=input, out=sys.stdout) -> dict:
    print(f"\n=== APPROVAL REQUIRED: {call.name} ===", file=out)
    try:
        print(json.dumps(json.loads(call.arguments), indent=2), file=out)
    except json.JSONDecodeError:
        print(call.arguments, file=out)
    if call.name == "execute_teardown":
        print("This PERMANENTLY deletes the listed AWS resources.", file=out)
    try:
        answer = input_fn("Approve? [y/N] ").strip().lower()
    except EOFError:  # no terminal (e.g. docker run without -t): nobody can approve, so deny
        print("\nno input available; denying", file=out)
        answer = ""
    decision = {"status": "allow"} if answer in ("y", "yes") else {"status": "deny", "reason": "denied by user"}
    return {"type": "user.tool_approval", "thread_id": call.thread_id, "tool_call_id": call.tool_call_id, "approval": decision}


def ask_question(call: PendingCall, input_fn=input, out=sys.stdout) -> dict:
    args = json.loads(call.arguments or "{}")
    print(f"\n? {args.get('question', '')}", file=out)
    for option in args.get("options") or []:
        print(f"   - {option}", file=out)
    return {"type": "user.tool_response", "thread_id": call.thread_id, "tool_call_id": call.tool_call_id, "content": input_fn("> ")}


def converse(client, session_id: str, message: str, input_fn=input, out=sys.stdout) -> TurnResult:
    """Send one user message, then keep resuming the turn for as long as the agent
    is paused waiting on an approval or an answer."""
    items = [{"type": "user.message", "content": message}]
    while True:
        result = stream_turn(client, session_id, items, out)
        if result.error:
            print(f"\n[turn error] {result.error}", file=out)
        items = [ask_approval(c, input_fn, out) for c in result.approvals] + \
                [ask_question(c, input_fn, out) for c in result.questions]
        if not items:
            return result


def open_session(client) -> str:
    return client.sessions.create(agent={"name": config.AGENT_NAME}).data.id


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--fresh"]
    fresh = config.FRESH_SESSION_PER_MESSAGE or "--fresh" in sys.argv[1:]
    client = config.make_client()
    session_id = open_session(client)
    mode = "new session per message (always fresh)" if fresh else "one session (agent re-queries by instruction)"
    print(f"session {session_id} on agent {config.AGENT_NAME!r} ({config.BASE_URL}); {mode}")

    if args:
        converse(client, session_id, " ".join(args))
        return
    print("Type a message (Ctrl-D or 'exit' to quit).")
    first = True
    while True:
        try:
            message = input("\nyou> ").strip()
        except EOFError:
            print()
            return
        if message in ("exit", "quit"):
            return
        if not message:
            continue
        if fresh and not first:
            session_id = open_session(client)
        first = False
        converse(client, session_id, message)


if __name__ == "__main__":
    main()
