"""Schedule the idle-resource scan, and review/approve what a scheduled run proposes.

    python -m janitor_agent schedule create [--name N] [--cron "0 9 * * *"] [--timezone Asia/Kolkata]
    python -m janitor_agent schedule list
    python -m janitor_agent schedule run-now|pause|resume|delete [--name N]
    python -m janitor_agent pending                 # scheduled runs waiting for your approval
    python -m janitor_agent review [SESSION_ID]     # see the report + costs, then approve or deny

A scheduled run scans for idle resources, writes a report with costs, and calls
execute_teardown, which PAUSES at the approval gate. Nothing is deleted until you
approve it here or in the TrueForge UI (Sessions -> the "Scheduled run").
"""
import argparse
import re
import sys

from janitor_agent import chat, config, scheduled_spec
from janitor_agent.register import find_agent

CRON_FIELD = re.compile(r"^[\d*/,\-]+$")


def validate_cron(cron: str) -> str:
    fields = cron.split()
    if len(fields) != 5 or not all(CRON_FIELD.match(f) for f in fields):
        raise ValueError(f"cron must be 5 fields like '0 9 * * *' (min hour day month weekday), got {cron!r}")
    return cron


def _manifest(cron: str, timezone: str, status: str) -> dict:
    return {"task": scheduled_spec.TASK, "cron": validate_cron(cron), "timezone": timezone, "status": status}


def find_schedule(client, name: str):
    return next((s for s in client.schedules.list() if s.name == name), None)


def _require(client, name: str):
    found = find_schedule(client, name)
    if found is None:
        sys.exit(f"no schedule named {name!r}; create it with: schedule create --name {name}")
    return found


def create_or_update(client, name: str, cron: str, timezone: str) -> str:
    """Idempotent: re-running with new values updates the existing schedule."""
    manifest = _manifest(cron, timezone, "active")
    existing = find_schedule(client, name)
    if existing is not None:
        client.schedules.update(schedule_id=existing.id, name=name, manifest=manifest)
        return f"updated schedule {name!r}: {cron} ({timezone})"
    client.schedules.create(agent_name=config.SCHEDULED_AGENT_NAME, name=name, manifest=manifest)
    return f"created schedule {name!r}: {cron} ({timezone}) -> agent {config.SCHEDULED_AGENT_NAME!r}"


def list_schedules(client) -> None:
    schedules = [s for s in client.schedules.list() if s.agent_name == config.SCHEDULED_AGENT_NAME]
    if not schedules:
        print("no schedules; create one with: schedule create")
    for s in schedules:
        print(f"{s.name}  cron='{s.manifest.cron}' tz={s.manifest.timezone} status={s.manifest.status}  id={s.id}")
        for run in list(client.schedules.list_runs(schedule_id=s.id))[:5]:
            print(f"    run {run.scheduled_for}  {run.status}" + (f"  ({run.reason})" if run.reason else ""))


def set_status(client, name: str, status: str) -> str:
    s = _require(client, name)
    client.schedules.update(schedule_id=s.id, name=s.name, manifest={**_manifest(s.manifest.cron, s.manifest.timezone, status)})
    return f"schedule {name!r} is now {status}"


def _awaiting_approval(turn) -> list:
    state = turn.state
    if getattr(state, "status", None) != "done":
        return []
    return [a for a in (state.required_actions or []) if a.type == "tool.approval_required"]


def latest_turn(client, session_id: str):
    """The session's most recent turn. Don't trust list order (it comes back oldest-first);
    turn ids are monotonic ULIDs, so the largest id is the newest."""
    return max(client.sessions.list_turns(session_id=session_id, limit=25), key=lambda t: t.id, default=None)


def pending_reviews(client, limit: int = 25) -> list[tuple]:
    """(session, turn, approval_events) for scheduled-agent sessions whose latest turn is paused for approval."""
    agent = find_agent(client, config.SCHEDULED_AGENT_NAME)
    if agent is None:
        return []
    found = []
    for session in client.sessions.list(agent_id=agent.id, limit=limit):
        turn = latest_turn(client, session.id)
        approvals = _awaiting_approval(turn) if turn is not None else []
        if approvals:
            found.append((session, turn, approvals))
    return found


def show_pending(client) -> None:
    found = pending_reviews(client)
    if not found:
        print("nothing is waiting for approval.")
    for session, _turn, approvals in found:
        print(f"{session.id}  {session.created_at}  {sum(len(a.tool_calls) for a in approvals)} call(s) awaiting approval")
    if found:
        print("\nreview one with: python -m janitor_agent review <SESSION_ID>")


def review(client, session_id: str | None, input_fn=input, out=sys.stdout) -> int:
    found = pending_reviews(client)
    if session_id:
        found = [f for f in found if f[0].id == session_id]
    if not found:
        print("nothing is waiting for approval" + (f" in session {session_id}." if session_id else "."), file=out)
        return 1
    session, turn, approvals = found[0]

    events = {e.id: e for e in client.sessions.list_turn_events(session_id=session.id, turn_id=turn.id, limit=100)}
    costs = chat.CostBook()
    print(f"=== Scheduled run {session.id} ({session.created_at}) ===", file=out)
    for event in sorted(events.values(), key=lambda e: e.id):
        if event.type == "tool.response":
            costs.learn(event.content)
        elif event.type == "model.message" and event.content and not event.tool_calls:
            print(event.content, file=out)

    calls = chat.resolve_pending(approvals, events)
    if not calls:
        print("could not read the pending tool call from this run.", file=out)
        return 1
    items = [chat.ask_approval(c, input_fn, out, costs) for c in calls]
    chat.settle(client, session.id, items, input_fn, out, costs)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="janitor_agent schedule")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "list", "run-now", "pause", "resume", "delete"):
        p = sub.add_parser(action)
        p.add_argument("--name", default=scheduled_spec.NAME_DEFAULT)
        if action == "create":
            p.add_argument("--cron", default="0 9 * * *", help="5-field cron, evaluated in --timezone (min interval 1h)")
            p.add_argument("--timezone", default=config.SCHEDULE_TIMEZONE)
    args = parser.parse_args(argv)
    client = config.make_client()

    if args.action == "create":
        print(create_or_update(client, args.name, args.cron, args.timezone))
    elif args.action == "list":
        list_schedules(client)
    elif args.action == "run-now":
        client.schedules.create_run(schedule_id=_require(client, args.name).id)
        print(f"triggered a run of {args.name!r}; see it with: pending / review")
    elif args.action in ("pause", "resume"):
        print(set_status(client, args.name, "paused" if args.action == "pause" else "active"))
    elif args.action == "delete":
        client.schedules.delete(schedule_id=_require(client, args.name).id)
        print(f"deleted schedule {args.name!r}")
    return 0
