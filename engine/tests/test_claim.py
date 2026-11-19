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

# --- the safety properties the reviewer demanded --------------------------------
# These are negative tests: they fail if someone later "simplifies" the guard away.

async def test_force_is_not_accepted_from_the_request_body(app_client):
    """A held task must not be stealable by putting force:true in the body.

    An earlier version read `force` straight out of the request and passed it to
    claim_task, which meant any caller could override another agent's ownership while the
    PR text claimed force was supervisor-only. Nothing in the HTTP surface may take a held
    task; reassignment is PATCH, which is auditable.
    """
    org = await make_org(app_client)
    ann = await make_agent(app_client, org["id"], "Ann")
    bob = await make_agent(app_client, org["id"], "Bob")
    task = await _task(app_client, org["id"])

    first = await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={"agentId": ann["id"]})
    assert first.status_code == 200, first.text

    steal = await app_client.post(f"/api/v1/tasks/{task['id']}/claim",
                                  json={"agentId": bob["id"], "force": True})
    assert steal.status_code == 409, (
        f"force must not be honoured from the body; got {steal.status_code}: {steal.text}")

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == ann["id"], "the original owner must survive a forced steal attempt"

async def test_claiming_without_naming_an_agent_is_refused(app_client):
    """A claim recorded against the user would be meaningless: the user assigns via PATCH."""
    org = await make_org(app_client)
    await make_agent(app_client, org["id"], "Ann")
    task = await _task(app_client, org["id"])

    r = await app_client.post(f"/api/v1/tasks/{task['id']}/claim", json={})
    assert r.status_code == 400, f"missing agentId must be refused, got {r.status_code}"

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id is None, "a refused claim must not assign anybody"

async def test_claiming_in_another_org_does_not_reach_a_foreign_task(app_client):
    """Two orgs must not see each other's tasks through the claim route."""
    ours = await make_org(app_client, "Ours")
    theirs = await make_org(app_client, "Theirs")
    ann = await make_agent(app_client, ours["id"], "Ann")
    await make_agent(app_client, theirs["id"], "Eve")
    task = await _task(app_client, ours["id"])

    # Eve claims via the claim-next route scoped to her own org: she must get nothing,
    # because our task belongs to another organisation.
    r = await app_client.post(f"/api/v1/orgs/{theirs['id']}/tasks/claim-next",
                              json={"agentId": "Eve"})
    assert r.status_code == 204, f"foreign org must not yield our task, got {r.status_code}"

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id is None, "our task must remain untouched"

async def test_claim_next_empty_queue_is_a_real_204_without_a_body(app_client):
    """204 must carry no body; a JSON payload with a 204 is a broken contract."""
    org = await make_org(app_client)
    await make_agent(app_client, org["id"], "Ann")

    r = await app_client.post(f"/api/v1/orgs/{org['id']}/tasks/claim-next",
                              json={"agentId": "Ann"})
    assert r.status_code == 204, r.text
    assert r.content == b"", f"204 must have an empty body, got {r.content!r}"

async def test_claiming_by_task_ref_works(app_client):
    """`_sole_org_id` must be awaited; a sync call there would break ref-based claiming."""
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    task = await _task(app_client, org["id"])

    r = await app_client.post(f"/api/v1/tasks/{task['ref']}/claim", json={"agentId": agent["id"]})
    assert r.status_code == 200, f"claiming by ref must resolve the org: {r.text}"
    assert r.json()["assigneeId"] == agent["id"]

# --- the tool must actually be offered, not merely registered -------------------
# The bug this pins: claim_task existed and answered over HTTP, so my endpoint tests were
# green, but `split()` never read TEAM_CORE, so the tool was deferred behind load_tools and
# agents never saw it. They went to the shell instead. "Registered" and "offered" are
# different facts and only the second one lets an agent act.

def test_claim_task_is_offered_without_loading_it_first():
    """In lean mode a coordination tool must be offered, not deferred."""
    from app.tools import loadout

    class _Org:
        settings: dict = {}

    tools = {name: object() for name in
             ["claim_task", "list_tasks", "update_task", "read_file", "write_file",
              "run_command", "search_files", "list_dir", "spawn_subagent", "todo",
              "load_tools", "stage_check", "render_preview", "browse_page", "post_message",
              "create_channel", "list_channels", "get_task", "create_task", "send_message",
              "update_org", "set_theme"]}
    offered, deferred = loadout.split(_Org(), tools, [], ())
    assert "claim_task" in offered, (
        "claim_task must be offered every turn; deferring it is why agents probed the API "
        f"instead. offered={offered} deferred={deferred}")

def test_team_core_is_read_by_split():
    """Guard against TEAM_CORE becoming dead code again."""
    from app.tools import loadout

    class _Org:
        settings: dict = {}

    # One tool from TEAM_CORE and enough other tools to stay in lean mode (> LEAN_ABOVE).
    tools = {name: object() for name in list(loadout.TEAM_CORE)}
    tools.update({f"filler_{i}": object() for i in range(loadout.LEAN_ABOVE + 5)})
    offered, _ = loadout.split(_Org(), tools, [], ())
    for name in loadout.TEAM_CORE:
        assert name in offered, f"{name} is in TEAM_CORE but split() did not offer it"
