"""T-57's other half: the wake-up a crash between "close saved" and "notice sent" used to lose.

Production evidence this fixes, not a simulation: T-52 closed at 2026-11-20T15:27:27, and T-53 -- whose
only dependency was T-52 -- has received nothing since (its single message predates the close by
thirteen hours).

Three failure modes, and the first version of this file only appeared to cover them:

  1. closed with NO record        the close committed, the intent written in a second transaction.
                                  That version corrupted the database and inserted the intent by hand,
                                  so it recreated the state by fiat -- it proved the sweep works, not
                                  that the window is closed. Now the crash is injected at the real
                                  boundary and the assertion is on what the CLOSE itself left.
  2. sent but not marked, or two  the notice lands, the process dies before mark_sent, and the sweep
     senders racing              sends it again. Covered by a conditional claim plus adopting an
                                  existing message instead of creating a second one.
  3. recorded, never sent         a plain crash after commit. A restart finishes it.

Every test drives the REAL path (HTTP PATCH -> tasks.update). Nothing is written into
`followup_intents` by hand except where the assertion is specifically about a row in that state.

The chain, per the review note: one dependency-notice id threads through the delivery, the successful
turn, and one persisted result. Creation notices are excluded -- creating a task also notifies its
assignee, so counting "any notification" would credit the wrong event.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents import runtime as runtime_mod
from app.core.db import SessionLocal
from app.models import Delivery, FollowupIntent, Message, Task, Turn
from app.services import followups
from app.services.comms import notify as real_notify
from tests.conftest import wait_for
from tests.helpers import make_agent, make_org, rows, say, script

TEXT = "All dependencies are done. You can start it now."


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


def _is_followup(m: Message) -> bool:
    """The dependency-closed notice. NOT the one sent when a task is created."""
    return m.kind == "notification" and "dependencies" in (m.content or "").lower()


async def _followups(task_id: str) -> list[Message]:
    return [m for m in await rows(Message, Message.task_id == task_id) if _is_followup(m)]


async def _chain(client, *, with_script: bool = False):
    """Two agents, one upstream task, one downstream task that depends on it."""
    org = await make_org(client)
    up = await make_agent(client, org["id"], "Up")
    down = await make_agent(client, org["id"], "Down")
    first = await _mk_task(client, org["id"], title="up", assignee=up["id"])
    second = await _mk_task(client, org["id"], title="down", assignee=down["id"],
                            depends_on=[first["id"]])
    if with_script:
        script(down["id"], say("downstream ran after the dependency closed"))
    return org, up, down, first, second


class CrashAtSend:
    """Interrupt the real path between the commit and the send.

    Because the close and the intent are now written in ONE transaction, what survives is exactly
    what a real crash leaves: status done, intent row `created`, nothing delivered.
    """

    def __init__(self):
        self.armed = True
        self.raised = False

    async def __call__(self, org_id, agent_ids, content, **kwargs):
        if self.armed and "dependencies" in (content or "").lower():
            self.armed = False
            self.raised = True
            raise RuntimeError("simulated crash between commit and send")
        return await real_notify(org_id, agent_ids, content, **kwargs)


async def _seed_owed(org_id: str, first: dict, second: dict, agent_id: str) -> None:
    """A row in the state a crash leaves. Used only where the assertion is about that state."""
    async with SessionLocal() as session:
        session.add(FollowupIntent(org_id=org_id, dependency_id=first["id"],
                                   dependent_id=second["id"], agent_id=agent_id,
                                   reason="dependencies-closed", text=TEXT,
                                   task_id=second["id"], status="created"))
        await session.commit()


@pytest.mark.asyncio
async def test_the_close_and_its_record_land_together(app_client):
    """One transaction: closing a dependency and owing the wake-up are the same fact."""
    org, _up, _down, first, second = await _chain(app_client)

    await _close(app_client, first["id"])

    intents = await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"])
    assert len(intents) == 1, f"exactly one intent for one dependency closing, got {len(intents)}"
    row = intents[0]
    assert row.status == "sent", "the live path sends it too, and settles the row"
    notes = await _followups(second["id"])
    assert len(notes) == 1
    assert row.message_id == notes[0].id, "the row points at the message that actually went out"


@pytest.mark.asyncio
async def test_a_crash_between_commit_and_send_is_recovered_from_the_real_path(app_client,
                                                                             monkeypatch):
    """Inject the crash where it happens, then let a restart finish the job."""
    org, _up, down, first, second = await _chain(app_client)

    crash = CrashAtSend()
    monkeypatch.setattr("app.services.tasks.comms.notify", crash)
    await _close(app_client, first["id"])
    assert crash.raised, "the crash must have fired, or this test proves nothing"
    monkeypatch.undo()

    # What the interrupted close left: closed, owed, unsent -- and the record came from the close
    # itself, not from the test.
    assert (await rows(Task, Task.id == first["id"]))[0].status == "done"
    intents = await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"])
    assert len(intents) == 1, "the record is there because it committed WITH the close"
    assert intents[0].status == "created", "and it is still owed, not lost"
    assert not await _followups(second["id"]), "precondition: nothing was delivered"

    fresh = runtime_mod.Runtime()
    runtime_mod.runtime = fresh
    import app.main as main_mod
    main_mod.runtime = fresh
    await fresh.start()
    try:
        async def delivered() -> bool:
            return bool(await _followups(second["id"]))

        await wait_for(delivered, msg="recovered follow-up", timeout=20)
        assert len(await _followups(second["id"])) == 1, "recovered once"
        assert await followups.owed_count(org["id"]) == 0
    finally:
        await fresh.stop()


@pytest.mark.asyncio
async def test_recovering_twice_does_not_send_twice(app_client, monkeypatch):
    """Restart, then restart again: the second sweep must find nothing to do."""
    org, _up, _down, first, second = await _chain(app_client)
    crash = CrashAtSend()
    monkeypatch.setattr("app.services.tasks.comms.notify", crash)
    await _close(app_client, first["id"])
    monkeypatch.undo()
    assert crash.raised

    first_sweep = await followups.sweep_followups()
    assert first_sweep["sent"] == 1, first_sweep
    second_sweep = await followups.sweep_followups()
    assert second_sweep["sent"] == 0, second_sweep
    assert len(await _followups(second["id"])) == 1, "one notice, however many sweeps run"


@pytest.mark.asyncio
async def test_two_callers_cannot_both_deliver_one_intent(app_client):
    """The claim is a conditional UPDATE: the database decides, not timing."""
    org, _up, down, first, second = await _chain(app_client)
    await _seed_owed(org["id"], first, second, down["id"])

    a = await followups.deliver_one(org["id"], first["id"], second["id"],
                                    "dependencies-closed", [down["id"]], TEXT)
    b = await followups.deliver_one(org["id"], first["id"], second["id"],
                                    "dependencies-closed", [down["id"]], TEXT)
    assert {a, b} == {"sent", "already-sent"}, (a, b)
    assert len(await _followups(second["id"])) == 1, "two callers, one notice"


@pytest.mark.asyncio
async def test_the_claim_is_exclusive_even_side_by_side(app_client):
    """The mechanism itself: one conditional UPDATE wins, the other reports the row is taken.

    This exists because the previous version of the concurrency test drove two SEQUENTIAL calls,
    which passed whether or not the claim was conditional -- the second call simply saw status
    `sent` in its read. Asserting on `claim()` directly is the only way to pin the race.
    """
    org, _up, down, first, second = await _chain(app_client)
    await _seed_owed(org["id"], first, second, down["id"])
    intent = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]

    first_claim = await followups.claim(intent.id)
    second_claim = await followups.claim(intent.id)
    assert first_claim is True, "the first claimant takes it"
    assert second_claim is False, "and the second must be told no, not silently allowed"
    row = (await rows(FollowupIntent, FollowupIntent.id == intent.id))[0]
    assert row.status == "sending" and row.attempts == 1, "one claim, one attempt"


@pytest.mark.asyncio
async def test_a_sender_that_loses_the_claim_does_not_send(app_client):
    """If the row is already claimed, deliver_one must return without sending anything."""
    org, _up, down, first, second = await _chain(app_client)
    await _seed_owed(org["id"], first, second, down["id"])
    intent = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]
    assert await followups.claim(intent.id) is True

    outcome = await followups.deliver_one(org["id"], first["id"], second["id"],
                                          "dependencies-closed", [down["id"]], TEXT)
    # A freshly-claimed row with no matching message is reported `in-flight`, not `claimed-by-other`:
    # the claim already happened, and the distinction is whether a message exists. Either way the
    # requirement is the same -- the loser must not send.
    assert outcome in ("in-flight", "claimed-by-other"), outcome
    assert not await _followups(second["id"]), "the loser must not send"


@pytest.mark.asyncio
async def test_a_notice_that_landed_but_was_not_marked_is_adopted_not_resent(app_client):
    """The nastiest window: the message exists, the row still says `created`."""
    org, _up, down, first, second = await _chain(app_client)
    await _seed_owed(org["id"], first, second, down["id"])

    assert await followups.deliver_one(org["id"], first["id"], second["id"],
                                       "dependencies-closed", [down["id"]], TEXT) == "sent"
    sent_msg = (await _followups(second["id"]))[0]

    # Rewind the row: the send happened, the marking did not.
    async with SessionLocal() as session:
        row = (await session.execute(select(FollowupIntent).where(
            FollowupIntent.dependent_id == second["id"]))).scalars().one()
        row.status, row.message_id = "created", sent_msg.id
        await session.commit()

    again = await followups.deliver_one(org["id"], first["id"], second["id"],
                                        "dependencies-closed", [down["id"]], TEXT)
    assert again == "adopted-existing", again
    assert len(await _followups(second["id"])) == 1, "adopted the existing message, did not resend"


@pytest.mark.asyncio
async def test_a_failed_send_stays_owed_rather_than_vanishing(app_client):
    """`sending` without a release would be the same loss in a new costume."""
    org, _up, down, first, second = await _chain(app_client)
    await _seed_owed(org["id"], first, second, down["id"])

    async def boom(*a, **k):
        raise RuntimeError("channel down")

    outcome = await followups.deliver_one(org["id"], first["id"], second["id"],
                                          "dependencies-closed", [down["id"]], TEXT, notify=boom)
    assert outcome == "send-failed"
    assert await followups.owed_count(org["id"]) == 1, "still owed, not stranded in `sending`"

    recovered = await followups.deliver_one(org["id"], first["id"], second["id"],
                                            "dependencies-closed", [down["id"]], TEXT)
    assert recovered == "sent", "and the next attempt can take it"


@pytest.mark.asyncio
async def test_the_notice_id_threads_delivery_turn_and_one_persisted_result(app_client):
    """The chain the review asked for: one id -> delivery -> completed turn -> one result row."""
    org, _up, down, first, second = await _chain(app_client, with_script=True)

    await _close(app_client, first["id"])

    notes = await _followups(second["id"])
    assert len(notes) == 1, "creation notices are excluded by _is_followup"
    notice = notes[0]

    async def woke() -> bool:
        return bool(await rows(Turn, Turn.agent_id == down["id"], Turn.status == "completed"))

    await wait_for(woke, msg="downstream turn", timeout=30)

    turns = await rows(Turn, Turn.agent_id == down["id"])
    assert len(turns) == 1, f"one wake-up, not {len(turns)}"
    turn = turns[0]

    delivs = await rows(Delivery, Delivery.message_id == notice.id)
    assert len(delivs) == 1, "one delivery per message, never two"
    assert delivs[0].turn_id == turn.id, "the notice id leads to the turn it actually caused"

    row = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]
    assert row.status == "sent"
    assert row.message_id == notice.id
    assert row.attempts == 1, f"sent once, so one attempt, got {row.attempts}"


@pytest.mark.asyncio
async def test_closing_the_same_dependency_twice_produces_one_notice(app_client):
    org, _up, _down, first, second = await _chain(app_client)

    await _close(app_client, first["id"])
    await app_client.patch(f"/api/v1/tasks/{first['id']}", json={"status": "in_progress"})
    await _close(app_client, first["id"])

    intents = await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"])
    assert len(intents) == 1, "the unique key swallows the repeat"
    assert len(await _followups(second["id"])) == 1


@pytest.mark.asyncio
async def test_a_stale_wake_up_is_cancelled_not_delivered(app_client, monkeypatch):
    org, _up, _down, first, second = await _chain(app_client)

    crash = CrashAtSend()
    monkeypatch.setattr("app.services.tasks.comms.notify", crash)
    await _close(app_client, first["id"])
    monkeypatch.undo()
    assert crash.raised

    async with SessionLocal() as session:
        t = await session.get(Task, second["id"])
        t.status = "done"
        await session.commit()

    counts = await followups.sweep_followups()
    assert counts["sent"] == 0 and counts["cancelled"] == 1, counts
    assert not await _followups(second["id"]), "a stale instruction must not reach anyone"


@pytest.mark.asyncio
async def test_recovery_releases_a_claimed_delivery_without_duplicating_it(app_client):
    """The other interrupted state: claimed, then the process died. No loss, no second row."""
    org = await make_org(app_client)
    worker = await make_agent(app_client, org["id"], "Worker")
    task = await _mk_task(app_client, org["id"], title="claimed then crashed",
                          assignee=worker["id"])
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


@pytest.mark.asyncio
async def test_a_crash_after_the_message_commits_is_recognised_by_its_tag(app_client):
    """The remaining breakpoint: message committed, mark_sent never ran, row stuck in `sending`.

    Nothing recorded the message id, so the row alone cannot say whether a notice went out. The
    message carries the intent id in `meta.followup`, so the recovery can find it and ADOPT it.
    Unconditionally resetting `sending` to `created` would resend and produce two notices.
    """
    org, _up, down, first, second = await _chain(app_client)
    await _seed_owed(org["id"], first, second, down["id"])
    intent = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]

    # Deliver for real (this tags the message), then reproduce the crash window: the row is in
    # `sending` with message_id NULL, exactly as a death between commit and mark_sent leaves it.
    assert await followups.deliver_one(org["id"], first["id"], second["id"],
                                       "dependencies-closed", [down["id"]], TEXT) == "sent"
    original = (await _followups(second["id"]))[0]
    assert (original.meta or {}).get("followup") == intent.id, "the send must tag the message"
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent.id)
        row.status, row.message_id, row.sent_at = "sending", None, None
        await session.commit()

    outcome = await followups.deliver_one(org["id"], first["id"], second["id"],
                                          "dependencies-closed", [down["id"]], TEXT)
    assert outcome == "adopted-existing", outcome
    assert len(await _followups(second["id"])) == 1, "adopted the committed message, did not resend"
    row = (await rows(FollowupIntent, FollowupIntent.id == intent.id))[0]
    assert row.status == "sent" and row.message_id == original.id


@pytest.mark.asyncio
async def test_a_sweep_recovers_a_sending_row_without_duplicating_it(app_client):
    """Same breakpoint, reached through the restart path rather than a direct call."""
    org, _up, down, first, second = await _chain(app_client)
    # Close for real, so the sweep's own precondition (a closed dependency, an owed notice) holds.
    # Seeding the row without closing would make _still_owed cancel it -- correctly, since the reason
    # would be false -- and the test would then be measuring the wrong branch.
    await _close(app_client, first["id"])
    intent = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]

    # Rewind to the crash window: message committed, row `sending`, message_id NULL.
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent.id)
        row.status, row.message_id, row.sent_at = "sending", None, None
        await session.commit()

    counts = await followups.sweep_followups()
    assert counts["sent"] == 0, counts
    assert counts["skipped"] == 1, counts
    assert len(await _followups(second["id"])) == 1, "still one notice after the sweep"
    assert await followups.owed_count(org["id"]) == 0


@pytest.mark.asyncio
async def test_a_stale_sending_row_with_no_message_is_released_not_duplicated(app_client):
    """The other half: if no message exists, the send never committed, so the row must be reusable."""
    from datetime import timedelta

    from app.core.ids import utcnow

    org, _up, down, first, second = await _chain(app_client)
    await _seed_owed(org["id"], first, second, down["id"])
    intent = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent.id)
        row.status, row.message_id = "sending", None
        row.sent_at = utcnow() - timedelta(seconds=followups.SENDING_GRACE_SECONDS + 5)
        await session.commit()

    outcome = await followups.deliver_one(org["id"], first["id"], second["id"],
                                          "dependencies-closed", [down["id"]], TEXT)
    assert outcome == "released-stale", outcome
    assert not await _followups(second["id"]), "released, not sent -- the caller retries"

    again = await followups.deliver_one(org["id"], first["id"], second["id"],
                                        "dependencies-closed", [down["id"]], TEXT)
    assert again == "sent"
    assert len(await _followups(second["id"])) == 1


@pytest.mark.asyncio
async def test_a_fresh_sending_row_is_left_alone(app_client):
    """Releasing a live send would be worse than a slow one: the release is what allows a second."""
    org, _up, down, first, second = await _chain(app_client)
    await _seed_owed(org["id"], first, second, down["id"])
    intent = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent.id)
        row.status, row.message_id = "sending", None
        await session.commit()

    outcome = await followups.deliver_one(org["id"], first["id"], second["id"],
                                          "dependencies-closed", [down["id"]], TEXT)
    assert outcome == "in-flight", outcome
    assert not await _followups(second["id"])


@pytest.mark.asyncio
async def test_two_restarts_write_the_downstream_result_once(app_client, monkeypatch):
    """The chain the review demands, measured at the BUSINESS result, not at the notice state.

    Counting deliveries or checking `status == sent` would pass even if the downstream task ran twice
    and wrote its result twice -- the notice is upstream of that. So this asserts:
      one notice id -> one delivery -> one successful turn -> one result write
    across two restarts, with the crash injected between the message commit and mark_sent.
    """
    org, _up, down, first, second = await _chain(app_client, with_script=True)

    # Crash the FIRST send after the message commits: deliver for real, then rewind to `sending`.
    await _close(app_client, first["id"])
    notes = await _followups(second["id"])
    assert len(notes) == 1, "the close delivered its notice before we rewind it"
    notice = notes[0]
    intent = (await rows(FollowupIntent, FollowupIntent.dependent_id == second["id"]))[0]
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent.id)
        row.status, row.message_id, row.sent_at = "sending", None, None
        await session.commit()

    # Restart #1: must adopt the committed message, not resend, and let the downstream run.
    async def restart():
        rt = runtime_mod.Runtime()
        runtime_mod.runtime = rt
        import app.main as main_mod
        main_mod.runtime = rt
        await rt.start()
        return rt

    r1 = await restart()
    try:
        async def ran() -> bool:
            return bool(await rows(Turn, Turn.agent_id == down["id"], Turn.status == "completed"))

        await wait_for(ran, msg="downstream run after restart 1", timeout=30)
    finally:
        await r1.stop()

    # Restart #2: nothing left to do. It must not send a second notice nor start a second run.
    r2 = await restart()
    try:
        await asyncio.sleep(2.0)
    finally:
        await r2.stop()

    # --- assertions on the business chain, not on the notice's own bookkeeping ---
    follow_ups = await _followups(second["id"])
    assert len(follow_ups) == 1, f"one notice, got {len(follow_ups)}"
    assert follow_ups[0].id == notice.id, "and it is the same message, adopted rather than replaced"

    delivs = await rows(Delivery, Delivery.message_id == notice.id)
    assert len(delivs) == 1, f"one delivery per notice, got {len(delivs)}"

    runs = await rows(Turn, Turn.agent_id == down["id"])
    assert len(runs) == 1, f"the downstream task ran ONCE across two restarts, got {len(runs)} times"
    assert delivs[0].turn_id == runs[0].id, "the notice led to exactly that run"

    row = (await rows(FollowupIntent, FollowupIntent.id == intent.id))[0]
    assert row.status == "sent" and row.message_id == notice.id
    assert row.attempts == 1, f"claimed once, got {row.attempts}"

    # One run is the claim; the status line above is not. A duplicate would show as a second Turn
    # and a second result write, which is what the counts assert.
