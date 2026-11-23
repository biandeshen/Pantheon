"""Why did T-3 (claimed, then done) not get an acceptance check while T-2 did?

Reproduces the two shapes side by side in a test database so the difference is measured, not guessed.
"""
from __future__ import annotations

import pytest

from app.models import Task, TaskEvent
from tests.conftest import wait_for
from tests.helpers import make_agent, make_org, rows, say, script, calls


async def _goal(client, org_id, agent_id, title="goal"):
    r = await client.post(f"/api/v1/orgs/{org_id}/tasks",
                          json={"title": title, "assigneeId": agent_id,
                                "acceptance": "one file exists"})
    assert r.status_code in (200, 201), r.text
    return r.json()


@pytest.mark.asyncio
async def test_a_goal_moved_to_done_by_its_assignee_gets_a_check(app_client):
    """The T-2 shape: no claim, straight to done."""
    org = await make_org(app_client)
    a = await make_agent(app_client, org["id"], "Ann")
    script(a["id"],
           calls(("write_file", {"path": "x.md", "content": "OK\n"})),
           calls(("update_task", {"task": "T-1", "result": "done it", "status": "done"})),
           say("done"))
    task = await _goal(app_client, org["id"], a["id"])

    async def checked() -> bool:
        return bool(await rows(TaskEvent, TaskEvent.task_id == task["id"],
                               TaskEvent.kind == "acceptance"))

    await wait_for(checked, msg="acceptance event", timeout=90)
    row = (await rows(Task, Task.id == task["id"]))[0]
    print("T-2 shape final status:", row.status)


@pytest.mark.asyncio
async def test_a_goal_claimed_then_completed_also_gets_a_check(app_client):
    """The T-3 shape: claim first, then finish. This is the case that did NOT get checked."""
    org = await make_org(app_client)
    a = await make_agent(app_client, org["id"], "Ann")
    script(a["id"],
           calls(("claim_task", {"task": "T-1"})),
           calls(("write_file", {"path": "y.md", "content": "OK\n"})),
           calls(("update_task", {"task": "T-1", "result": "done it", "status": "done"})),
           say("done"))
    task = await _goal(app_client, org["id"], a["id"])

    async def claimed() -> bool:
        return bool(await rows(TaskEvent, TaskEvent.task_id == task["id"],
                               TaskEvent.kind == "claimed"))

    await wait_for(claimed, msg="claim", timeout=60)

    async def checked() -> bool:
        return bool(await rows(TaskEvent, TaskEvent.task_id == task["id"],
                               TaskEvent.kind == "acceptance"))

    await wait_for(checked, msg="acceptance event after claim", timeout=120)
