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


class CostBook:
    """Remembers what analyze_infrastructure said each flagged resource costs, straight from
    the tool output (never from the model), so the approval prompt can show what a delete
    would save. Estimates come from the MCP server's PRICING.md rates."""

    CATEGORIES = ("orphaned_volumes", "idle_instances", "running_instances", "idle_load_balancers")

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def learn(self, tool_output: str) -> None:
        try:
            data = json.loads(tool_output)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        for category in self.CATEGORIES:
            for item in data.get(category) or []:
                rid = item.get("id") or item.get("arn")
                if rid:
                    self.items[rid] = {
                        "kind": category,
                        "name": item.get("name") or (item.get("tags") or {}).get("Name") or "",
                        "cost": item.get("estimated_monthly_cost_usd"),
                        "complete": bool(item.get("cost_complete")),
                    }

    def describe(self, resource_ids: list[str]) -> list[str]:
        lines, total, complete = [], 0.0, True
        for rid in resource_ids:
            info = self.items.get(rid)
            if info is None:
                lines.append(f"  {rid}: cost unknown (not seen in a scan this session)")
                complete = False
                continue
            cost = info["cost"]
            if cost is None:
                lines.append(f"  {rid} {info['name']}: not priced (no rate)")
            else:
                mark = "" if info["complete"] else " + unpriced parts"
                lines.append(f"  {rid} {info['name']}: ${cost:,.2f}/month{mark}")
                total += cost
            complete = complete and info["complete"]
        lines.append(f"  Estimated monthly savings: ${total:,.2f}" + ("" if complete else " (at least; some costs not priced)"))
        lines.append("  (estimate from PRICING.md us-east-1 list-price rates, not live AWS billing)")
        return lines

    def savings_line(self, teardown_output: str) -> str | None:
        try:
            deleted = [d["id"] for d in json.loads(teardown_output).get("deleted", [])]
        except (TypeError, ValueError, AttributeError, KeyError):
            return None
        return "\n".join(self.describe(deleted)) if deleted else None


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


def stream_turn(client, session_id: str, items: list[dict], out=sys.stdout, costs: CostBook | None = None) -> TurnResult:
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
            if costs is not None:
                costs.learn(event.content)
                if (saved := costs.savings_line(event.content)) is not None:
                    print(f"\n  Freed by this teardown:\n{saved}", file=out)
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


def ask_approval(call: PendingCall, input_fn=input, out=sys.stdout, costs: CostBook | None = None) -> dict:
    print(f"\n=== APPROVAL REQUIRED: {call.name} ===", file=out)
    try:
        print(json.dumps(json.loads(call.arguments), indent=2), file=out)
    except json.JSONDecodeError:
        print(call.arguments, file=out)
    if call.name == "execute_teardown":
        print("This PERMANENTLY deletes the listed AWS resources.", file=out)
        if costs is not None:
            try:
                ids = json.loads(call.arguments).get("resource_ids", [])
            except (json.JSONDecodeError, AttributeError):
                ids = []
            print("What deleting these would save:", file=out)
            print("\n".join(costs.describe(ids)), file=out)
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


def settle(client, session_id: str, items: list[dict], input_fn=input, out=sys.stdout, costs: CostBook | None = None) -> TurnResult:
    """Run a turn with `items`, then keep resuming it for as long as the agent is paused
    waiting on an approval or an answer from you."""
    while True:
        result = stream_turn(client, session_id, items, out, costs)
        if result.error:
            print(f"\n[turn error] {result.error}", file=out)
        items = [ask_approval(c, input_fn, out, costs) for c in result.approvals] + \
                [ask_question(c, input_fn, out) for c in result.questions]
        if not items:
            return result


def converse(client, session_id: str, message: str, input_fn=input, out=sys.stdout, costs: CostBook | None = None) -> TurnResult:
    """Send one user message, then settle any approvals/questions it triggers."""
    return settle(client, session_id, [{"type": "user.message", "content": message}], input_fn, out, costs)


def open_session(client) -> str:
    return client.sessions.create(agent={"name": config.AGENT_NAME}).data.id


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--fresh"]
    fresh = config.FRESH_SESSION_PER_MESSAGE or "--fresh" in sys.argv[1:]
    client = config.make_client()
    session_id = open_session(client)
    costs = CostBook()
    mode = "new session per message (always fresh)" if fresh else "one session (agent re-queries by instruction)"
    print(f"session {session_id} on agent {config.AGENT_NAME!r} ({config.BASE_URL}); {mode}")

    if args:
        converse(client, session_id, " ".join(args), costs=costs)
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
            costs = CostBook()
        first = False
        converse(client, session_id, message, costs=costs)


if __name__ == "__main__":
    main()
