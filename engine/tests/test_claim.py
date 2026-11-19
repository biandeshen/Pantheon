"""Claiming a task: one agent takes it, and a second agent cannot take it too.

The point of these tests is the race, not the happy path. Two agents claiming the same
task concurrently must leave exactly one owner. If both succeed, the loser starts work on
something someone else is already doing and nobody finds out until the results disagree.
"""

from __future__ import annotations

import asyncio

from app.models import Task, TaskEvent
from tests.helpers import make_agent, make_org, rows

async def _task(client, org_id: str, title: str = "Write the market brief") -> dict:
    r = await client.post(f"/api/v1/orgs/{org_id}/tasks",
                          json={"title": title, "description": "d", "status": "todo"})
    assert r.status_code == 201, r.text
    return r.json()

async def test_claiming_an_unassigned_task_takes_it_and_records_the_event(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    task = await _task(app_client, org["id"])

    r = await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": agent["id"]})
    assert r.status_code == 200, r.text
    claimed = r.json()
    assert claimed["assigneeId"] == agent["id"]
    assert claimed["status"] == "in_progress"

    events = await rows(TaskEvent, TaskEvent.task_id == task["id"], TaskEvent.kind == "claimed")
    assert len(events) == 1, "a claim must leave exactly one event behind"
    assert events[0].data["to"] == agent["id"]

async def test_a_second_claim_is_refused_and_the_first_owner_kept(app_client):
    org = await make_org(app_client)
    first = await make_agent(app_client, org["id"], "Ann")
    second_agent = await make_agent(app_client, org["id"], "Bob")
    task = await _task(app_client, org["id"])

    r1 = await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": first["id"]})
    assert r1.status_code == 200, r1.text
    r2 = await app_client.post(f"/api/v1/tasks/{task['id']}/claim",
                               json={"agentId": second_agent["id"]})
    assert r2.status_code == 409, f"second claim must be refused, got {r2.status_code}: {r2.text}"

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == first["id"], "the refused claim must not steal the task"

async def test_concurrent_claims_leave_exactly_one_owner(app_client):
    """The real case: two agents ask at the same instant.

    Both requests are in flight together. Whatever order the database serialises them in,
    one must win and the other must be told it lost. A test that claims sequentially would
    pass even if `with_for_update()` were removed.
    """
    org = await make_org(app_client)
    ann = await make_agent(app_client, org["id"], "Ann")
    bob = await make_agent(app_client, org["id"], "Bob")
    task = await _task(app_client, org["id"])

    results = await asyncio.gather(
        app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": ann["id"]}),
        app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": bob["id"]}),
        return_exceptions=True,
    )
    codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert codes == [200, 409], f"expected exactly one winner, got {codes}"

    events = await rows(TaskEvent, TaskEvent.task_id == task["id"], TaskEvent.kind == "claimed")
    assert len(events) == 1, "a lost race must not leave a second claim event"

async def test_claiming_a_closed_task_is_refused(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    task = await _task(app_client, org["id"])
    r = await app_client.patch(f"/api/v1/tasks/{task['id']}", json={"status": "done"})
    assert r.status_code == 200, r.text

    r = await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": agent["id"]})
    assert r.status_code == 409

async def test_claiming_what_you_already_hold_is_idempotent(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    task = await _task(app_client, org["id"])

    first = await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": agent["id"]})
    assert first.status_code == 200
    again = await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": agent["id"]})
    assert again.status_code == 200, "re-claiming your own task must not be an error"
    assert again.json()["assigneeId"] == agent["id"]

    events = await rows(TaskEvent, TaskEvent.task_id == task["id"], TaskEvent.kind == "claimed")
    assert len(events) == 1, "an idempotent re-claim must not double-write the event"

async def test_claim_next_picks_a_free_task_and_skips_a_held_one(app_client):
    """Autonomous work needs 'give me something', not 'claim this exact id'."""
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    first = await _task(app_client, org["id"], "Task one")
    second = await _task(app_client, org["id"], "Task two")

    taken = await app_client.post(f"/api/v1/tasks/{first['id']}/claim", json={"agentId": agent["id"]})
    assert taken.status_code == 200, taken.text

    r = await app_client.post(f"/api/v1/orgs/{org['id']}/tasks/claim-next", json={"agentId": agent["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == second["id"], "claim-next must not return the already-held task"

async def test_claim_next_reports_nothing_free_without_inventing_a_task(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")

    r = await app_client.post(f"/api/v1/orgs/{org['id']}/tasks/claim-next", json={"agentId": agent["id"]})
    assert r.status_code == 204, f"an empty queue must be 204, not a fabricated task: {r.text}"

async def test_a_task_waiting_on_a_dependency_cannot_be_claimed(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    blocker = await _task(app_client, org["id"], "Blocker")
    dependent = await app_client.post(
        f"/api/v1/orgs/{org['id']}/tasks",
        json={"title": "Dependent", "description": "d", "status": "todo",
              "dependsOn": [blocker["ref"]]})
    assert dependent.status_code == 201, dependent.text
    dep = dependent.json()

    r = await app_client.post(f"/api/v1/tasks/{dep['id']}/claim", json={"agentId": agent["id"]})
    assert r.status_code == 409, "work must not start on a task whose dependency is open"
    assert blocker["ref"] in r.text
