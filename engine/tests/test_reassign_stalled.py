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
