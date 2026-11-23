"""Probe: is POST /orgs/{org}/tasks with an assignee the native dispatch entry?

Not a fixture replay -- it asks whether creating a task is what puts work INTO the engine's own
queue (delivery + wake), as opposed to starting some external runner.
"""
from __future__ import annotations

import pytest

from app.models import Delivery, Message, Task, Turn
from tests.conftest import wait_for
from tests.helpers import make_agent, make_org, rows, say, script


@pytest.mark.asyncio
async def test_creating_a_task_with_an_assignee_is_the_native_dispatch(app_client):
    org = await make_org(app_client)
    who = await make_agent(app_client, org["id"], "Ann")
    script(who["id"], say("picked it up"))

    r = await app_client.post(f"/api/v1/orgs/{org['id']}/tasks",
                              json={"title": "native dispatch probe", "assigneeId": who["id"],
                                    "acceptance": "produces something"})
    assert r.status_code in (200, 201), r.text
    task = r.json()

    # 1. the task exists in the engine's own store with the assignee bound
    row = (await rows(Task, Task.id == task["id"]))[0]
    assert row.assignee_id == who["id"]

    # 2. a notification message carries the task id -- that is what ties work to the queue
    msgs = [m for m in await rows(Message, Message.task_id == task["id"])]
    assert msgs, "creating with an assignee must notify, or nothing can wake"

    # 3. a delivery exists and is picked up (native claim by the assigned agent)
    async def delivered() -> bool:
        ds = await rows(Delivery, Delivery.message_id == msgs[0].id)
        return bool(ds) and ds[0].status in ("done", "claimed")

    await wait_for(delivered, msg="delivery picked up", timeout=30)
    ds = await rows(Delivery, Delivery.message_id == msgs[0].id)
    assert len(ds) == 1, f"one delivery per notice, got {len(ds)}"

    # 4. a Run started from it -- the engine's own execution record, not an external process
    async def ran() -> bool:
        return bool(await rows(Turn, Turn.agent_id == who["id"]))

    await wait_for(ran, msg="run started", timeout=30)
    runs = await rows(Turn, Turn.agent_id == who["id"])
    assert len(runs) == 1, f"one run, got {len(runs)}"


@pytest.mark.asyncio
async def test_a_task_with_open_dependencies_is_queued_without_waking(app_client):
    """The dispatch entry deliberately does NOT start work that waits on others."""
    org = await make_org(app_client)
    a = await make_agent(app_client, org["id"], "Up")
    b = await make_agent(app_client, org["id"], "Down")

    first = (await app_client.post(f"/api/v1/orgs/{org['id']}/tasks",
                                   json={"title": "dep", "assigneeId": a["id"]})).json()
    second = (await app_client.post(f"/api/v1/orgs/{org['id']}/tasks",
                                    json={"title": "waits", "assigneeId": b["id"],
                                          "dependsOn": [first["id"]]})).json()

    # the dependent got a notice (it is news), but no run: wake=False while deps are open
    msgs = await rows(Message, Message.task_id == second["id"])
    assert msgs, "the assignee should still be told"
    assert not await rows(Turn, Turn.agent_id == b["id"]), (
        "a task waiting on an open dependency must not start -- an early start works on code "
        "that is not there yet")
