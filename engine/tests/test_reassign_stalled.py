"""Reassigning a task away from a holder that can no longer work.

The gap this closes was found by an agent, not by reading the code: the platform supervisor
asked load_tools for `reassign_task` / `set_task_assignee` and was told
`ERROR: you have no tool named reassign_task, set_task_assignee, ...`. It could *see* the
stalled task (it has list_tasks, get_task, org_activity) and could not act on it.

Two safeguards, each of which alone would be a hole:

- supervisor only, or any agent could take any task by calling this;
- the holder must be unable to work, or this becomes a stealth way to steal live work --
  exactly what claim_task's 409 exists to prevent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Agent, Task
from app.tools import collab
from app.tools.base import ToolContext, ToolError
from tests.helpers import make_agent, make_org, rows


async def _ctx(org_id: str, agent_id: str) -> ToolContext:
    from app.core.db import SessionLocal
    from app.models import Org

    async with SessionLocal() as s:
        o = (await s.execute(select(Org).where(Org.id == org_id))).scalar_one()
        a = (await s.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    return ToolContext(org=o, agent=a, turn_id=None, tool_call_id="t", depth=0)


async def _task(app_client, org_id: str, title: str = "stalled work") -> dict:
    r = await app_client.post(f"/api/v1/orgs/{org_id}/tasks",
                              json={"title": title, "description": "d", "status": "todo"})
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _make_supervisor(app_client, org_id: str, name: str) -> dict:
    """Create an agent and promote it the only way the platform allows.

    The HTTP API deliberately strips is_supervisor from both create and patch, so that no
    caller can escalate an agent through the API -- that is a real guard, not an oversight.
    Built-in supervisors are seeded. Here the flag is set directly to stand in for that
    seeded state; the tool under test still has to check it.
    """
    r = await app_client.post(f"/api/v1/orgs/{org_id}/agents",
                              json={"name": name, "role": "Platform supervisor"})
    assert r.status_code in (200, 201), r.text
    a = r.json()

    from app.core.db import SessionLocal

    async with SessionLocal() as s:
        agent = await s.get(Agent, a["id"])
        agent.is_supervisor = True
        await s.commit()
        await s.refresh(agent)
        assert agent.is_supervisor is True
    return {"id": a["id"], "name": name}


async def test_a_supervisor_can_take_a_task_from_a_disabled_holder(app_client):
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "Holder")
    taker = await make_agent(app_client, org["id"], "Taker")
    sup = await _make_supervisor(app_client, org["id"], "Boss")
    task = await _task(app_client, org["id"])

    # Holder claims it, then can no longer work.
    r = await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    assert r.status_code == 200, r.text
    r = await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})
    assert r.status_code == 200, r.text

    ctx = await _ctx(org["id"], sup["id"])
    out = await collab.reassign_stalled_task(
        {"task": task["ref"], "to": "Taker", "reason": "holder disabled"}, ctx)
    assert "Reassigned" in out, out

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == taker["id"], "the task must move to the named agent"
    assert stored.status == "in_progress", "a taken-over task is being worked, not restarted"


async def test_the_partial_result_survives_the_reassignment(app_client):
    """The whole point of taking over rather than restarting: the work done so far is kept."""
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "Holder2")
    await make_agent(app_client, org["id"], "Taker2")
    sup = await _make_supervisor(app_client, org["id"], "Boss2")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    r = await app_client.patch(f"/api/v1/tasks/{task['id']}",
                               json={"comment": "half-done: found three of five sources"})
    assert r.status_code == 200, r.text
    await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})

    ctx = await _ctx(org["id"], sup["id"])
    await collab.reassign_stalled_task(
        {"task": task["ref"], "to": "Taker2", "reason": "holder disabled"}, ctx)

    r = await app_client.get(f"/api/v1/tasks/{task['id']}")
    body = r.text
    assert "half-done: found three of five sources" in body, (
        "the previous holder's partial result must still be there after a takeover")


async def test_it_refuses_to_steal_from_a_healthy_holder(app_client):
    """Otherwise this is just claim_task's 409 with a supervisor-shaped hole in it."""
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "Busy")
    await make_agent(app_client, org["id"], "Other")
    sup = await _make_supervisor(app_client, org["id"], "Boss3")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    # holder stays active

    ctx = await _ctx(org["id"], sup["id"])
    with pytest.raises(ToolError) as e:
        await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "Other", "reason": "want it"}, ctx)
    assert "stalled" in str(e.value).lower() or "active" in str(e.value).lower(), e.value

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == holder["id"], "a healthy holder keeps their work"


async def test_a_non_supervisor_cannot_reassign(app_client):
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "H4")
    thief = await make_agent(app_client, org["id"], "Thief")
    await make_agent(app_client, org["id"], "Target4")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})

    ctx = await _ctx(org["id"], thief["id"])
    with pytest.raises(ToolError) as e:
        await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "Target4", "reason": "give it to me"}, ctx)
    assert "supervisor" in str(e.value).lower(), e.value

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == holder["id"]


async def test_it_says_so_when_the_task_has_no_holder(app_client):
    org = await make_org(app_client)
    await make_agent(app_client, org["id"], "H5")
    sup = await _make_supervisor(app_client, org["id"], "Boss5")
    task = await _task(app_client, org["id"])

    ctx = await _ctx(org["id"], sup["id"])
    with pytest.raises(ToolError) as e:
        await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "H5", "reason": "nobody has it"}, ctx)
    assert "no assignee" in str(e.value).lower(), e.value


# --- three guards added after review, each one a mistake in the first version -----------
# All three were found by reading the code against real states, not by a failing test. The
# tests are written so that removing the guard fails them.

async def test_a_holder_waiting_for_approval_is_not_stalled(app_client):
    """awaiting_approval means blocked on a person, not broken.

    The first version listed awaiting_approval as a stall reason. That would let a supervisor
    take work from an agent that is mid-task and simply waiting for a human to answer -- and
    strand the work it had already done.
    """
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "Waiter")
    await make_agent(app_client, org["id"], "Other6")
    sup = await _make_supervisor(app_client, org["id"], "Boss6")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    # Simulate "its turn is blocked on an approval" without needing a real shell request.
    from app.core.db import SessionLocal

    async with SessionLocal() as s:
        h = await s.get(Agent, holder["id"])
        h.runtime_status = "awaiting_approval"
        await s.commit()

    ctx = await _ctx(org["id"], sup["id"])
    with pytest.raises(ToolError) as e:
        await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "Other6", "reason": "looks stuck"}, ctx)
    assert "approval" in str(e.value).lower(), e.value

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == holder["id"], "a holder blocked on approval keeps its task"


async def test_a_task_in_review_is_never_reassigned(app_client):
    """Review is waiting on a reviewer. Taking it would discard the submitted work."""
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "Submitter")
    await make_agent(app_client, org["id"], "Other7")
    sup = await _make_supervisor(app_client, org["id"], "Boss7")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    r = await app_client.patch(f"/api/v1/tasks/{task['id']}",
                               json={"status": "review", "result": "done, please review"})
    assert r.status_code == 200, r.text
    await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})

    ctx = await _ctx(org["id"], sup["id"])
    with pytest.raises(ToolError) as e:
        await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "Other7", "reason": "holder gone"}, ctx)
    assert "review" in str(e.value).lower(), e.value

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.status == "review", "a submitted task stays submitted"
    assert stored.result, "and its submitted result is not wiped"


async def test_a_closed_task_is_never_reopened_by_a_handover(app_client):
    """done is finished business. Reopening is its own decision, not a side effect."""
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "Finisher")
    await make_agent(app_client, org["id"], "Other8")
    sup = await _make_supervisor(app_client, org["id"], "Boss8")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    r = await app_client.patch(f"/api/v1/tasks/{task['id']}",
                               json={"status": "done", "result": "finished and checked"})
    assert r.status_code == 200, r.text
    await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})

    ctx = await _ctx(org["id"], sup["id"])
    with pytest.raises(ToolError) as e:
        await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "Other8", "reason": "holder gone"}, ctx)
    assert "done" in str(e.value).lower() or "finished" in str(e.value).lower(), e.value

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.status == "done", "a finished task must stay finished"


async def test_the_reassignment_writes_an_event_with_the_reason(app_client):
    """The move is auditable: who lost it, who took it, and why."""
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "Gone")
    taker = await make_agent(app_client, org["id"], "Next")
    sup = await _make_supervisor(app_client, org["id"], "Boss9")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})

    ctx = await _ctx(org["id"], sup["id"])
    await collab.reassign_stalled_task(
        {"task": task["ref"], "to": "Next", "reason": "holder disabled mid-work"}, ctx)

    from app.models import TaskEvent

    evs = await rows(TaskEvent, TaskEvent.task_id == task["id"], TaskEvent.kind == "assignee")
    assert evs, "the reassignment must be in the event log"
    last = evs[-1]
    assert last.actor_id == sup["id"], "the supervisor is the actor, not the taker"
    payload = last.data if isinstance(last.data, dict) else {}
    assert payload.get("to") == taker["id"], f"the new holder is recorded: {payload}"
    assert payload.get("from") == holder["id"], "and the previous holder is recorded"
    assert "disabled" in (payload.get("why") or ""), "the stall reason is recorded"
    assert payload.get("reason") == "holder disabled mid-work", "the operator's reason is kept"


async def test_the_assignment_is_committed_before_the_note_is_written(app_client):
    """The move and its record are separate writes, on purpose, in that order.

    If the note were written first and the move then failed, the log would claim a handover
    that never happened. If the move happened and the note failed, the task is still correctly
    moved and only the explanation is missing -- the less harmful failure. This test pins the
    order by checking the assignment is durable even when the comment path is what breaks it.
    """
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "DeadA")
    taker = await make_agent(app_client, org["id"], "NextA")
    sup = await _make_supervisor(app_client, org["id"], "BossA")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})

    from app.core.db import SessionLocal
    from app.services import tasks as tasks_svc

    original = tasks_svc.update_task

    async def explode(*a, **kw):
        # only the note write goes through update_task in the new implementation
        raise RuntimeError("comment path unavailable")

    tasks_svc.update_task = explode
    try:
        ctx = await _ctx(org["id"], sup["id"])
        out = await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "NextA", "reason": "holder disabled"}, ctx)
        assert "Reassigned" in out
    finally:
        tasks_svc.update_task = original

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == taker["id"], (
        "the handover must be durable even when the explanatory note cannot be written")


# --- assign permission is not edit permission -------------------------------------------
# Found in review of #2, and it was a real hole: the port from update_task kept check_assign
# and dropped check_edit. An agent allowed to hand work to someone is not automatically
# allowed to take a task away from its owner.

async def test_a_supervisor_who_may_assign_but_may_not_edit_is_refused(app_client):
    """Two different permissions, two different questions.

    The org policy here says assign=anyone (the default) but edit=assigned, so the supervisor
    can tell whose task it should become and still has no right to touch this particular task.
    Both must be checked; passing the first is not consent for the second.
    """
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "OwnerX")
    taker = await make_agent(app_client, org["id"], "NextX")
    sup = await _make_supervisor(app_client, org["id"], "BossX")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})

    # assign: anyone (default) -- but edit limited to tasks the agent is involved in, which
    # this supervisor is not.
    r = await app_client.patch(f"/api/v1/orgs/{org['id']}",
                               json={"settings": {"task_policy": {"assign": "anyone",
                                                                  "edit": "assigned"}}})
    assert r.status_code == 200, r.text

    before = await rows(Task, Task.id == task["id"])
    assert before[0].assignee_id == holder["id"]

    ctx = await _ctx(org["id"], sup["id"])
    with pytest.raises(ToolError) as e:
        await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "NextX", "reason": "holder disabled"}, ctx)
    msg = str(e.value).lower()
    assert "may not change" in msg or "not a task" in msg or "isn't" in msg, e.value

    after = (await rows(Task, Task.id == task["id"]))[0]
    assert after.assignee_id == holder["id"], "a refused reassignment writes no assignee"
    assert after.status == "in_progress", "and leaves the status alone"

    from app.models import TaskEvent

    evs = await rows(TaskEvent, TaskEvent.task_id == task["id"], TaskEvent.kind == "assignee")
    assert not evs, "and records no event that could be mistaken for a handover"


async def test_the_permission_error_becomes_a_tool_error_not_a_crash(app_client):
    """An agent must see a sentence it can act on, not a ValueError traceback."""
    org = await make_org(app_client)
    holder = await make_agent(app_client, org["id"], "OwnerY")
    await make_agent(app_client, org["id"], "NextY")
    sup = await _make_supervisor(app_client, org["id"], "BossY")
    task = await _task(app_client, org["id"])

    await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": holder["id"]})
    await app_client.patch(f"/api/v1/agents/{holder['id']}", json={"status": "disabled"})
    await app_client.patch(f"/api/v1/orgs/{org['id']}",
                           json={"settings": {"task_policy": {"edit": "assigned"}}})

    ctx = await _ctx(org["id"], sup["id"])
    try:
        await collab.reassign_stalled_task(
            {"task": task["ref"], "to": "NextY", "reason": "x"}, ctx)
    except ToolError:
        pass  # expected: the model gets a message it can read
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"should surface as ToolError, got {type(e).__name__}: {e}") from None
    else:
        raise AssertionError("should have been refused")
