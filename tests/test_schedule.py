"""Offline tests for scheduling and review, using fake TrueForge objects."""
import io
import json
from types import SimpleNamespace as NS

import pytest

from janitor_agent import chat, config, schedule, scheduled_spec, spec


def test_scheduled_agent_is_unattended_idle_only_and_gated():
    manifest = scheduled_spec.manifest()
    text = manifest["instructions"]
    assert "include_running=false" in text and "never running instances" in text
    assert "NEVER calculate or invent costs" in text and "do not retry" in text
    assert manifest["config"]["ask_user_questions"] == {"enabled": False}  # nobody is there to answer
    assert manifest["mcp_servers"][0]["require_approval_for_tools"] == ["execute_teardown"]  # deletion still gated
    assert manifest["model"] == spec.manifest()["model"]


@pytest.mark.parametrize("cron", ["0 9 * * *", "*/30 8-18 * * 1-5", "0 0 1 * *"])
def test_valid_cron_accepted(cron):
    assert schedule.validate_cron(cron) == cron


@pytest.mark.parametrize("cron", ["", "0 9 * *", "0 9 * * * *", "every day", "0 9 * * MON"])
def test_invalid_cron_rejected(cron):
    with pytest.raises(ValueError, match="5 fields"):
        schedule.validate_cron(cron)


class FakeSchedules:
    def __init__(self, existing=()):
        self.items, self.created, self.updated = list(existing), [], []

    def list(self):
        return self.items

    def create(self, **kw):
        self.created.append(kw)

    def update(self, **kw):
        self.updated.append(kw)


def test_create_is_idempotent_and_updates_an_existing_schedule():
    client = NS(schedules=FakeSchedules())
    assert "created" in schedule.create_or_update(client, "nightly", "0 2 * * *", "UTC")
    assert client.schedules.created[0]["agent_name"] == config.SCHEDULED_AGENT_NAME
    assert client.schedules.created[0]["manifest"]["task"] == scheduled_spec.TASK

    client = NS(schedules=FakeSchedules([NS(id="s1", name="nightly")]))
    assert "updated" in schedule.create_or_update(client, "nightly", "0 3 * * *", "UTC")
    assert client.schedules.created == [] and client.schedules.updated[0]["schedule_id"] == "s1"


def _turn(tid, status="done", actions=()):
    return NS(id=tid, state=NS(status=status, required_actions=list(actions)))


APPROVAL = NS(type="tool.approval_required", thread_id="main", tool_calls=[NS(id="c1", source_event_id="m1")])


class FakeSessions:
    def __init__(self, turns_by_session, events=()):
        self.turns_by_session, self.events = turns_by_session, list(events)

    def list(self, **_):
        return [NS(id=sid, created_at="2026-09-26T09:00:00Z") for sid in self.turns_by_session]

    def list_turns(self, session_id, limit):
        return self.turns_by_session[session_id]  # oldest-first, like the real API

    def list_turn_events(self, **_):
        return self.events


def _client(turns_by_session, events=()):
    agents = NS(list=lambda agent_name: [NS(id="a1", name=agent_name)])
    return NS(agents=agents, sessions=FakeSessions(turns_by_session, events))


def test_pending_uses_the_latest_turn_not_the_first_one():
    client = _client({
        "resolved": [_turn("01", actions=[APPROVAL]), _turn("02")],   # paused once, then approved/denied
        "waiting": [_turn("01"), _turn("02", actions=[APPROVAL])],    # paused on its latest turn
        "running": [_turn("01", status="running")],
    })
    assert [s.id for s, _t, _a in schedule.pending_reviews(client)] == ["waiting"]


def test_review_shows_report_and_costs_then_resumes_with_the_decision(monkeypatch):
    scan = json.dumps({"orphaned_volumes": [{"id": "vol-1", "estimated_monthly_cost_usd": 8.0, "cost_complete": True, "tags": {"Name": "old"}}]})
    events = [
        NS(id="e1", type="tool.response", content=scan),
        NS(id="e2", type="model.message", content="Found 1 idle volume, $8.00/month.", tool_calls=None),
        NS(id="m1", type="model.message", content="", tool_calls=[NS(id="c1", function=NS(name="execute_teardown", arguments='{"resource_ids": ["vol-1"]}'))]),
    ]
    client = _client({"s1": [_turn("01", actions=[APPROVAL])]}, events)
    resumed = []
    monkeypatch.setattr(chat, "settle", lambda client, sid, items, input_fn, out, costs: resumed.append((sid, items)))
    out = io.StringIO()

    assert schedule.review(client, "s1", input_fn=lambda _: "y", out=out) == 0

    shown = out.getvalue()
    assert "Found 1 idle volume" in shown and "$8.00/month" in shown and "Estimated monthly savings: $8.00" in shown
    [(sid, items)] = resumed
    assert sid == "s1" and items[0]["type"] == "user.tool_approval" and items[0]["approval"] == {"status": "allow"}


def test_review_with_nothing_pending_says_so_and_does_not_resume(monkeypatch):
    client = _client({"s1": [_turn("01")]})
    monkeypatch.setattr(chat, "settle", lambda *a, **k: pytest.fail("must not resume"))
    out = io.StringIO()
    assert schedule.review(client, None, out=out) == 1
    assert "nothing is waiting" in out.getvalue()
