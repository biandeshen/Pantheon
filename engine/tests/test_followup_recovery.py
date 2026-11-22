"""T-57's other half: the wake-up that a crash between "close saved" and "notice sent" used to lose.

Production evidence this fixes, not a simulation: T-52 closed at 2026-11-20T15:27:27, and T-53 -- the
task whose only dependency was T-52 -- has received nothing since. Its single message predates the
close by thirteen hours. So the window is real and the loss is real.

Two things must hold, and they are opposites, so both are asserted:

  1. the wake-up is not lost   -> a close whose notice never went out is finished by the sweep
  2. the wake-up is not doubled -> a notice that did go out is never sent again, and the same
                                  dependency closing twice produces one notice

Note on scope: this is the engine's behaviour against a test database. The production task T-53 is
NOT used as a restart reproduction -- it has no assignee, so no wake-up was ever possible for it, and
claiming otherwise would be inventing evidence.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agents import runtime as runtime_mod
from app.core.db import SessionLocal
from app.models import Delivery, FollowupIntent, Message, Task
from app.services import followups
from tests.conftest import wait_for
from tests.helpers import make_agent, make_org, rows


async def _mk_task(client, org_id: str, *, title: str, assignee: str | None, depends_on=None) -> dict:
    body: dict = {"title": title}
    if assignee:
        body["assigneeId"] = assignee
    if depends_on:
        body["dependsOn"] = depends_on
    r = await client.post(f"/api/v1/orgs/{org_id}/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _close(client, task_id: str, *, result: str = "accepted") -> None:
    r = await client.patch(f"/api/v1/tasks/{task_id}", json={"status": "done", "result": result})
    assert r.status_code == 200, r.text


async def _followup_messages(task_id: str) -> list[Message]:
    msgs = await rows(Message, Message.task_id == task_id)
    return [m for m in msgs if m.kind == "notification" and "dependencies" in (m.content or "").lower()]


@pytest.mark.asyncio
async def test_a_close_records_the_intent_before_it_sends(app_client):
    """The durable record must exist and end up `sent`, tied to the message that was delivered."""
    org = await make_org(app_client)
    up = await make_agent(app_client, org["id"], "Up")
    down = await make_agent(app_client, org["id"], "Down")
    first = await _mk_task(app_client, org["id"], title="up", assignee=up["id"])
    second = await _mk_task(app_client, org["id"], title="down", assignee=down["id"],
                            depends_on=[first["id"]])

    await _close(app_client, first["id"])

    intents = await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"])
    assert len(intents) == 1, f"exactly one intent for one dependency closing, got {len(intents)}"
    row = intents[0]
    assert row.status == "sent"
    assert row.message_id, "and it points at the notice that was actually delivered"
    assert row.reason == "dependencies-closed"

    notes = await _followup_messages(second["id"])
    assert len(notes) == 1
    assert row.message_id == notes[0].id


@pytest.mark.asyncio
async def test_the_interrupted_close_is_finished_by_the_sweep_exactly_once(app_client):
    """Crash between the two: status committed, no notice, intent left `created`."""
    org = await make_org(app_client)
    up = await make_agent(app_client, org["id"], "Up")
    down = await make_agent(app_client, org["id"], "Down")
    first = await _mk_task(app_client, org["id"], title="up", assignee=up["id"])
    second = await _mk_task(app_client, org["id"], title="down", assignee=down["id"],
                            depends_on=[first["id"]])

    # Reproduce the window: close in the database (the commit that survives) and record the intent,
    # but never send. That is exactly the state a process death here leaves behind.
    async with SessionLocal() as session:
        t = await session.get(Task, first["id"])
        t.status = "done"
        t.result = "accepted"
        await session.commit()
    await followups.record_followups(org["id"], [{
        "dependency_id": first["id"], "dependent_id": second["id"], "agent_id": down["id"],
        "reason": "dependencies-closed",
        "text": f"All dependencies of {second['ref']} are done. You can start it now.",
        "task_id": second["id"],
    }])

    assert await followups.owed_count(org["id"]) == 1
    assert not await _followup_messages(second["id"]), "precondition: nothing was sent"

    counts = await followups.sweep_followups()
    assert counts["sent"] == 1, counts
    assert await followups.owed_count(org["id"]) == 0

    notes = await _followup_messages(second["id"])
    assert len(notes) == 1, "the lost wake-up is recovered"

    # ...and a second sweep must not send it again.
    counts2 = await followups.sweep_followups()
    assert counts2["sent"] == 0, "already sent, so nothing more is owed"
    assert len(await _followup_messages(second["id"])) == 1, "and it was not delivered twice"


@pytest.mark.asyncio
async def test_closing_the_same_dependency_twice_produces_one_notice(app_client):
    """The unique key is the exactly-once guarantee; re-recording must be a no-op, not a duplicate."""
    org = await make_org(app_client)
    up = await make_agent(app_client, org["id"], "Up")
    down = await make_agent(app_client, org["id"], "Down")
    first = await _mk_task(app_client, org["id"], title="up", assignee=up["id"])
    second = await _mk_task(app_client, org["id"], title="down", assignee=down["id"],
                            depends_on=[first["id"]])

    intent = {"dependency_id": first["id"], "dependent_id": second["id"], "agent_id": down["id"],
              "reason": "dependencies-closed", "text": "deps done", "task_id": second["id"]}
    made1 = await followups.record_followups(org["id"], [intent])
    made2 = await followups.record_followups(org["id"], [intent])
    assert len(made1) == 1
    assert len(made2) == 0, "the second record hits the unique key and is skipped, not duplicated"
    assert len(await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"])) == 1


@pytest.mark.asyncio
async def test_a_stale_wake_up_is_cancelled_not_delivered(app_client):
    """If the dependent is already closed, telling someone to start it would be wrong."""
    org = await make_org(app_client)
    up = await make_agent(app_client, org["id"], "Up")
    down = await make_agent(app_client, org["id"], "Down")
    first = await _mk_task(app_client, org["id"], title="up", assignee=up["id"])
    second = await _mk_task(app_client, org["id"], title="down", assignee=down["id"],
                            depends_on=[first["id"]])
    await followups.record_followups(org["id"], [{
        "dependency_id": first["id"], "dependent_id": second["id"], "agent_id": down["id"],
        "reason": "dependencies-closed", "text": "start now", "task_id": second["id"]}])

    async with SessionLocal() as session:
        t = await session.get(Task, second["id"])
        t.status = "done"
        await session.commit()

    counts = await followups.sweep_followups()
    assert counts["sent"] == 0 and counts["cancelled"] == 1, counts
    assert not await _followup_messages(second["id"]), "a stale instruction must not reach anyone"
    row = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]
    assert row.status == "cancelled"


@pytest.mark.asyncio
async def test_a_restart_finishes_what_the_crash_left_owed(app_client):
    """The real path: nothing calls the sweep by hand -- starting the runtime does it."""
    org = await make_org(app_client)
    up = await make_agent(app_client, org["id"], "Up")
    down = await make_agent(app_client, org["id"], "Down")
    first = await _mk_task(app_client, org["id"], title="up", assignee=up["id"])
    second = await _mk_task(app_client, org["id"], title="down", assignee=down["id"],
                            depends_on=[first["id"]])

    async with SessionLocal() as session:
        t = await session.get(Task, first["id"])
        t.status = "done"
        await session.commit()
    await followups.record_followups(org["id"], [{
        "dependency_id": first["id"], "dependent_id": second["id"], "agent_id": down["id"],
        "reason": "dependencies-closed", "text": "All dependencies are done. You can start it now.", "task_id": second["id"]}])
    assert await followups.owed_count(org["id"]) == 1

    fresh = runtime_mod.Runtime()
    runtime_mod.runtime = fresh
    import app.main as main_mod
    main_mod.runtime = fresh
    await fresh.start()
    try:
        async def delivered() -> bool:
            return bool(await _followup_messages(second["id"]))

        await wait_for(delivered, msg="recovered follow-up after restart", timeout=20)
        assert len(await _followup_messages(second["id"])) == 1
        assert await followups.owed_count(org["id"]) == 0
        # And the delivery exists, so the wake-up reaches an agent rather than only a table.
        msgs = await _followup_messages(second["id"])
        ds = await rows(Delivery, Delivery.message_id == msgs[0].id)
        assert ds and all(d.status in ("pending", "done", "claimed") for d in ds)
    finally:
        await fresh.stop()


@pytest.mark.asyncio
async def test_recovery_releases_a_claimed_delivery_without_duplicating_it(app_client):
    """The other interrupted state: claimed, then the process died. No loss, no second row."""
    org = await make_org(app_client)
    worker = await make_agent(app_client, org["id"], "Worker")
    task = await _mk_task(app_client, org["id"], title="claimed then crashed", assignee=worker["id"])
    r = await app_client.post(f"/api/v1/orgs/{org['id']}/messages",
                              json={"to": "Worker", "content": "do it"})
    assert r.status_code == 201

    async with SessionLocal() as session:
        msgs = (await session.execute(
            select(Message).where(Message.task_id == task["id"]))).scalars().all()
        for m in msgs:
            for d in (await session.execute(
                    select(Delivery).where(Delivery.message_id == m.id))).scalars().all():
                d.status, d.lease_until, d.turn_id = "claimed", None, None
        await session.commit()

    fresh = runtime_mod.Runtime()
    runtime_mod.runtime = fresh
    import app.main as main_mod
    main_mod.runtime = fresh
    await fresh.start()
    try:
        async def released() -> bool:
            async with SessionLocal() as session:
                msgs = (await session.execute(
                    select(Message).where(Message.task_id == task["id"]))).scalars().all()
                ids = [m.id for m in msgs]
                ds = (await session.execute(
                    select(Delivery).where(Delivery.message_id.in_(ids)))).scalars().all()
                return bool(ds) and all(d.status != "claimed" for d in ds)

        await wait_for(released, msg="claimed deliveries released", timeout=20)
        async with SessionLocal() as session:
            msgs = (await session.execute(
                select(Message).where(Message.task_id == task["id"]))).scalars().all()
            ids = [m.id for m in msgs]
            ds = (await session.execute(
                select(Delivery).where(Delivery.message_id.in_(ids)))).scalars().all()
        assert len(ds) == len({d.message_id for d in ds}), "one delivery per message, never two"
    finally:
        await fresh.stop()
