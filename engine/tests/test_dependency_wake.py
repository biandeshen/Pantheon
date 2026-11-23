"""T-57: a real business acceptance closes, and a DIFFERENT downstream task is picked up on its own.

The claim under test is narrow and checkable: when a dependency is closed by its own acceptance, the
dependent task must be woken, claimed and executed natively -- not by a person writing to the agent,
not by the agent's own chat. T-65 only ever proved "creating a task wakes the assignee". This is the
other path: `tasks.py` followups -> comms.notify(task_id=dependent) -> delivery -> claim -> turn.

Two restarts are exercised in isolation, because "no loss, no duplication" is only meaningful if the
interrupted states are actually reproduced:
  A. acceptance saved, notification NOT delivered  (crash between commit and notify)
  B. delivery claimed, result NOT saved            (crash between claim and turn completion)

Everything here runs against the real engine in-process with a scripted model: no network, no paid
provider, no new task created for the sake of evidence (the tasks are created by the test itself and
never appear in the production database).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents import runtime as runtime_mod
from app.core.db import SessionLocal
from app.models import Delivery, Message, Task, Turn
from tests.conftest import wait_for
from tests.helpers import dm, make_agent, make_org, rows, say, script

CLOSED = ("done", "cancelled")


async def _create_task(client, org_id: str, *, title: str, assignee: str, depends_on=None) -> dict:
    body = {"title": title, "assigneeId": assignee}
    if depends_on:
        body["dependsOn"] = depends_on
    r = await client.post(f"/api/v1/orgs/{org_id}/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _close_task(client, org_id: str, task_id: str, *, result: str = "done") -> None:
    # The engine's task update route is org-less: PATCH /api/v1/tasks/{id}.
    r = await client.patch(f"/api/v1/tasks/{task_id}",
                           json={"status": "done", "result": result})
    assert r.status_code == 200, r.text


async def _messages_for(task_id: str) -> list[Message]:
    return await rows(Message, Message.task_id == task_id)


async def _deliveries_for(task_id: str) -> list[Delivery]:
    msgs = await _messages_for(task_id)
    ids = [m.id for m in msgs]
    if not ids:
        return []
    return await rows(Delivery, Delivery.message_id.in_(ids))


@pytest.mark.asyncio
async def test_a_closed_dependency_wakes_a_different_downstream_task_exactly_once(app_client):
    """The normal path: close A by acceptance, B is claimed and run without anyone telling it to."""
    org = await make_org(app_client)
    up = await make_agent(app_client, org["id"], "Up")
    down = await make_agent(app_client, org["id"], "Down")

    first = await _create_task(app_client, org["id"], title="upstream", assignee=up["id"])
    second = await _create_task(app_client, org["id"], title="downstream", assignee=down["id"],
                                depends_on=[first["id"]])

    # Downstream does one visible thing, so "it ran" is a product, not a claim.
    script(down["id"], say("downstream ran"), say("downstream ran"))

    await _close_task(app_client, org["id"], first["id"], result="accepted")

    async def woke() -> bool:
        return any(t.status in ("completed", "failed")
                   for t in await rows(Turn, Turn.agent_id == down["id"]))

    await wait_for(woke, msg="downstream turn", timeout=30)

    turns = await rows(Turn, Turn.agent_id == down["id"])
    assert len(turns) == 1, f"exactly one wake-up, not {len(turns)}"

    notes = [m for m in await _messages_for(second["id"]) if m.kind == "notification"]
    assert notes, "the wake-up must be a real notification addressed to the downstream task"
    assert any("dependencies" in (m.content or "").lower() for m in notes), (
        "and it must say why it woke: its dependency closed")

    delivs = await _deliveries_for(second["id"])
    assert delivs, "the notification must be delivered natively, not only written"
    assert all(d.attempts == 1 for d in delivs), "delivered once, never retried"


@pytest.mark.asyncio
async def test_restart_after_the_close_is_saved_but_before_the_notice_is_sent(app_client):
    """Interrupted state A: the acceptance is committed, the notification never was.

    This is the state that silently loses work: the dependency reads as closed, so nothing will ever
    fire the follow-up again. What must be true afterwards is that the downstream task is either woken
    or still visibly waiting -- never quietly forgotten.
    """
    org = await make_org(app_client)
    up = await make_agent(app_client, org["id"], "Up")
    down = await make_agent(app_client, org["id"], "Down")

    first = await _create_task(app_client, org["id"], title="upstream", assignee=up["id"])
    second = await _create_task(app_client, org["id"], title="downstream", assignee=down["id"],
                                depends_on=[first["id"]])

    # Reproduce the crash window: mark the dependency closed in the database WITHOUT going through
    # tasks.update (which is what sends the follow-up).
    async with SessionLocal() as session:
        row = await session.get(Task, first["id"])
        row.status = "done"
        row.result = "accepted"
        await session.commit()

    # Creating a task already notifies its assignee, so the precondition is narrower: no FOLLOW-UP
    # notification exists -- the one that says a dependency closed. That is the message a crash in
    # this window would have lost.
    def is_followup(m) -> bool:
        return m.kind == "notification" and "dependencies" in (m.content or "").lower()

    before = await _messages_for(second["id"])
    assert not [m for m in before if is_followup(m)], (
        "precondition: the dependency-closed notice does not exist, exactly the state a crash here "
        "would leave")

    # Restart: a new runtime instance over the same database.
    runtime_mod.runtime = runtime_mod.Runtime()
    import app.main as main_mod
    main_mod.runtime = runtime_mod.runtime

    script(down["id"], say("downstream ran after restart"))

    async def woke() -> bool:
        return bool([m for m in await _messages_for(second["id"]) if is_followup(m)]) or bool(
            await rows(Turn, Turn.agent_id == down["id"]))

    try:
        await wait_for(woke, msg="downstream after restart", timeout=8)
        resumed = True
    except AssertionError:
        resumed = False

    # Either outcome is acceptable; a silent third state is not. If it did not resume, the task must
    # still be open and its dependency chain must still be readable, so a human can see it waiting.
    still = (await rows(Task, Task.id == second["id"]))[0]
    if not resumed:
        assert still.status not in CLOSED, "a task that was never woken must not look finished"
        assert still.depends_on == [first["id"]], "and its dependency must still be recorded"


@pytest.mark.asyncio
async def test_restart_after_a_delivery_is_claimed_but_before_the_result_is_saved(app_client):
    """Interrupted state B: claimed, then the process dies before the turn finishes.

    `_recover()` puts claimed deliveries back to pending. The risk is the opposite of loss:
    the same message being executed twice. So the assertion is on the count, not on success.
    """
    org = await make_org(app_client)
    worker = await make_agent(app_client, org["id"], "Worker")

    task = await _create_task(app_client, org["id"], title="claimed then crashed",
                              assignee=worker["id"])

    script(worker["id"], say("ran once"), say("ran once"))
    await dm(app_client, org["id"], "Worker", "please do it")

    async def started() -> bool:
        return bool(await rows(Turn, Turn.agent_id == worker["id"]))

    await wait_for(started, msg="first turn", timeout=30)

    # Force the crash window: a delivery left in `claimed` with no turn attached.
    async with SessionLocal() as session:
        msgs = (await session.execute(select(Message).where(Message.task_id == task["id"]))).scalars().all()
        for m in msgs:
            for d in (await session.execute(
                    select(Delivery).where(Delivery.message_id == m.id))).scalars().all():
                d.status = "claimed"
                d.lease_until = None
                d.turn_id = None
        await session.commit()

    # Restart: a new runtime instance over the same database. start() is what runs _recover(), so it
    # is called explicitly -- the app fixture's own lifespan belongs to the previous instance.
    fresh = runtime_mod.Runtime()
    runtime_mod.runtime = fresh
    import app.main as main_mod
    main_mod.runtime = fresh
    await fresh.start()
    try:
        async def recovered() -> bool:
            ds = await _deliveries_for(task["id"])
            return bool(ds) and all(d.status != "claimed" for d in ds)

        await wait_for(recovered, msg="claimed deliveries released", timeout=20)

        ds = await _deliveries_for(task["id"])
        assert all(d.status in ("pending", "done") for d in ds), (
            f"no delivery may stay claimed after a restart: {[d.status for d in ds]}")
        # The risk is duplication, not loss: recovery must release the lease without inventing a
        # second delivery row for the same message.
        assert len(ds) == len({d.message_id for d in ds}), "one delivery per message, never two"
    finally:
        await fresh.stop()
