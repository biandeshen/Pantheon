"""Follow-ups that survive a crash between "the close is saved" and "the notice is sent".

The bug, measured in production rather than simulated: T-52 closed at 2026-11-20T15:27:27, and T-53 --
whose only dependency was T-52 -- has received nothing since (its single message predates the close by
thirteen hours).

Three failure modes have to be closed, and only the third one is about crashes:

  1. closed with NO record        the close committed in one transaction, the intent written in
                                  another. Fixed at the writer: `tasks.update` now adds the intent
                                  rows to the same session as the status change, so the pair is
                                  atomic and "closed with no record" stops being reachable.
  2. recorded but NEVER sent      the row stays `created`. `deliver_one` sends it now;
                                  `sweep_followups` sends it after a restart.
  3. sent but NOT MARKED, or two  a notice lands, the process dies before `mark_sent`, and the sweep
     senders racing               sends it again -- or a live send and a sweep both go. So the claim
                                  is a conditional UPDATE that only one caller can win
                                  (status='created' -> 'sending'), and the message is looked up by
                                  the intent's own key before sending, never guessed.

The unique key (dependency, dependent, reason) is the "exactly once" anchor: a task closed twice, or
a sweep racing a live send, cannot produce two notices for the same dependency closing.

Scope note: this is engine behaviour verified against a test database. Production task T-53 is NOT
used as a restart reproduction -- it has no assignee, so no wake-up was ever possible for it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update

from app.core.db import SessionLocal
from app.core.ids import utcnow
from app.models import FollowupIntent, Task

log = logging.getLogger(__name__)

CLOSED = ("done", "cancelled")


async def record_followups(session, org_id: str, intents: list[dict]) -> list[FollowupIntent]:
    """Add intent rows to an EXISTING session, so they commit with whatever the caller is doing.

    Deliberately takes the caller's session instead of opening its own: the point is that the close
    and the record of what it owes cannot be separated, and a second transaction would put the gap
    back. Duplicates (same dependency, dependent, reason) are skipped rather than raising, because
    "already owed" is a normal outcome -- a task closed twice, a retried request.
    """
    made: list[FollowupIntent] = []
    for it in intents:
        exists = (await session.execute(select(FollowupIntent).where(
            FollowupIntent.dependency_id == it["dependency_id"],
            FollowupIntent.dependent_id == it["dependent_id"],
            FollowupIntent.reason == it["reason"],
        ))).scalar_one_or_none()
        if exists is not None:
            continue
        row = FollowupIntent(
            org_id=org_id, dependency_id=it["dependency_id"],
            dependent_id=it["dependent_id"], agent_id=it.get("agent_id"),
            reason=it["reason"], text=it["text"], task_id=it.get("task_id"),
            status="created",
        )
        session.add(row)
        made.append(row)
    return made


async def claim(intent_id: str, allow_cancelled: bool = False) -> bool:
    """Take exclusive responsibility for sending one intent. False means someone else has it.

    A conditional UPDATE, not a read-then-write: two callers (live send and sweep, or two sweeps)
    both run this statement, and the database decides. Without it, "sent exactly once" held only
    because restarts happened to be rare.
    """
    async with SessionLocal() as session:
        reissuable = ("created", "cancelled") if allow_cancelled else ("created",)
        res = await session.execute(
            update(FollowupIntent)
            .where(FollowupIntent.id == intent_id, FollowupIntent.status.in_(reissuable))
            .values(status="sending", attempts=FollowupIntent.attempts + 1)
        )
        await session.commit()
        return (res.rowcount or 0) == 1


async def mark_sent(intent_id: str, message_id: str | None) -> None:
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent_id)
        if row is None:
            return
        row.status = "sent"
        row.message_id = message_id
        row.sent_at = utcnow()
        await session.commit()


async def release(intent_id: str) -> None:
    """Put a claimed-but-unsent intent back, so the next sweep can try again.

    Used when the send raised. Leaving it in `sending` would be a silent loss -- the row would never
    be picked up again, which is the same failure in a new costume.
    """
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent_id)
        if row is None or row.status != "sending":
            return
        row.status = "created"
        await session.commit()


async def cancel(intent_id: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent_id)
        if row is None or row.status not in ("created", "sending"):
            return
        row.status = "cancelled"
        await session.commit()


async def _existing_message(row: FollowupIntent):
    """Did this intent's notice already get created? Then never create a second one.

    Two ways to know, and the second is the one that matters after a crash:

      row.message_id set   the normal path -- it was marked after sending
      row in `sending`, message_id NULL, but a message carrying this intent's id in `meta.followup`
                            the process died AFTER the message was committed and BEFORE mark_sent.
                            Nothing recorded the message id, so looking only at the column finds
                            nothing and a naive retry sends a second notice.

    The meta tag is written by deliver_one on every send, so the message itself is the record. This is
    why "unconditionally reset `sending` to `created` and resend" is wrong: `sending` with a matching
    message means "sent, not yet marked", and the honest recovery is to adopt it.
    """
    from app.models import Message

    async with SessionLocal() as session:
        if row.message_id:
            got = await session.get(Message, row.message_id)
            if got is not None:
                return got
        return await _find_tagged(session, row)


# How long a `sending` row is trusted before it is assumed dead. Generous on purpose: releasing a
# live send would be worse than a slow one, because the release is what allows a second message.
SENDING_GRACE_SECONDS = 120


def _is_stale(row: FollowupIntent) -> bool:
    """Is a `sending` row old enough that no live worker can still own it?"""
    from datetime import timedelta

    when = row.sent_at or row.created_at
    if when is None:
        return True
    return utcnow() - when > timedelta(seconds=SENDING_GRACE_SECONDS)


async def _find_tagged(session, row: FollowupIntent):
    """A message that carries this intent's id, whatever the row's status says.

    Scans the dependent task's notifications and checks meta.followup. Bounded by the task's own
    notifications, which is a handful even on a busy task.
    """
    from app.models import Message

    rows_ = (await session.execute(
        select(Message).where(Message.kind == "notification",
                              Message.task_id == row.dependent_id)
    )).scalars().all()
    for m in rows_:
        if (m.meta or {}).get("followup") == row.id:
            return m
    return None


async def deliver_one(org_id: str, dependency_id: str, dependent_id: str, reason: str,
                      targets: list[str], text: str, *, depth: int = 0, notify=None,
                      reissue_cancelled: bool = False) -> str:
    """Deliver one already-recorded intent. Returns the outcome for the caller's log.

    Idempotent by construction: it claims first, looks for an existing message second, and only then
    sends. Anything that stops it mid-way leaves the row recoverable rather than duplicated.
    """
    from app.services import comms

    send = notify or comms.notify
    async with SessionLocal() as session:
        row = (await session.execute(select(FollowupIntent).where(
            FollowupIntent.dependency_id == dependency_id,
            FollowupIntent.dependent_id == dependent_id,
            FollowupIntent.reason == reason,
        ))).scalar_one_or_none()
        if row is None:
            return "no-intent"
        intent_id = row.id
        if row.status == "sent":
            return "already-sent"
        if row.status == "cancelled" and not reissue_cancelled:
            return "cancelled"
        if row.status == "cancelled" and reissue_cancelled:
            # The caller (sweep) already re-checked that the conditions hold again. Recorded as a
            # re-issue so a reader can see this notice was owed twice, not sent twice in a row.
            row.status, row.sent_at = "created", utcnow()
            await session.commit()

    # A row stuck in `sending` is the crash-after-commit case, and it must be handled BEFORE the
    # claim -- `claim` only accepts `created`, so asking it first would report "someone else has it"
    # forever and the notice would never be delivered or adopted.
    #
    # Three outcomes, and they are not interchangeable:
    #   tagged message exists   -> sent but unmarked: adopt it, do NOT resend
    #   no message, still fresh -> another worker may be mid-send right now: leave it alone
    #   no message, stale       -> the send never committed: release it so a later attempt can run
    if row.status == "sending":
        tagged = await _existing_message(row)
        if tagged is not None:
            await mark_sent(intent_id, tagged.id)
            return "adopted-existing"
        if not _is_stale(row):
            return "in-flight"
        await release(intent_id)
        return "released-stale"

    if not await claim(intent_id, allow_cancelled=reissue_cancelled):
        return "claimed-by-other"

    row = await _row(intent_id)
    already = await _existing_message(row) if row else None
    if already is not None:
        # A previous attempt did create the message but died before marking it. Adopt it instead of
        # sending a second one: the notice exists, and "exists twice" is worse than "marked late".
        await mark_sent(intent_id, already.id)
        return "adopted-existing"

    try:
        # task_id is not decoration: it is how the notice is found later (and how a reader can tell
        # a dependency wake-up from the notice a task gets when it is created). Dropping it here was
        # a real bug -- the row said `sent`, and the message existed with no task to look it up by.
        # The tag is the durable link between a message and the intent that caused it. Without it a
        # crash after this line and before mark_sent leaves a delivered notice nobody can identify,
        # and the recovery has to choose between losing it and sending it twice.
        msg = await send(org_id, targets, text, depth=depth, task_id=dependent_id,
                         meta={"followup": intent_id})
    except Exception:  # noqa: BLE001 - the caller must keep going; the row stays owed
        log.exception("follow-up %s could not be sent; releasing it", intent_id)
        await release(intent_id)
        return "send-failed"
    await mark_sent(intent_id, getattr(msg, "id", None))
    return "sent"


async def _row(intent_id: str) -> FollowupIntent | None:
    async with SessionLocal() as session:
        return await session.get(FollowupIntent, intent_id)


async def _still_owed(row: FollowupIntent, dependent: Task | None,
                      dependency: Task | None, session=None) -> tuple[bool, str]:
    """Is the reason still true, NOW?

    The earlier version only glanced at the one dependency that happened to trigger it. Measured
    problem: a task with two dependencies could be woken when the first closed and the second was
    still open -- the notice says "all dependencies are done", which would then be false. So the
    whole dependency set is checked, not the single edge that fired.

    Cancelled rows come back through here too: if a notice was cancelled because the conditions
    lapsed and they later hold again, it is legitimately owed once more (the "sent" status is what
    forbids a second delivery, not "cancelled").
    """
    if dependent is None:
        return False, "dependent task is gone"
    if dependent.status in CLOSED:
        return False, "dependent is already closed"
    if not dependent.assignee_id:
        return False, "dependent has no assignee to wake"
    if row.reason == "dependencies-closed":
        if dependency is not None and dependency.status not in CLOSED:
            return False, "dependency was reopened"
        # Every dependency, not just this one. A partial rollout here would tell someone "all done"
        # while another prerequisite is still open -- the notice would be a lie the moment it lands.
        if session is not None and dependent.depends_on:
            for dep_id in dependent.depends_on:
                other = await session.get(Task, dep_id)
                if other is not None and other.status not in CLOSED:
                    return False, f"another dependency is still open ({other.id[-6:]})"
    if row.reason == "child-stop" and dependent.assignee_id is None:
        return False, "child has no assignee"
    return True, ""


async def sweep_followups(notify=None) -> dict[str, int]:
    """Send what is owed, cancel what no longer applies. Counts go to the log and the tests."""
    sent = cancelled = failed = skipped = 0
    async with SessionLocal() as session:
        # `sending` too: a row left in that state by a crash is exactly what the sweep exists for.
        # deliver_one decides whether it is adoptable, in flight, or stale -- the sweep does not guess.
        # `cancelled` is included on purpose. A notice cancelled because its conditions lapsed is not
        # the same as one that was delivered: if the conditions hold again, it is owed once. `sent`
        # is the only status that permanently forbids another delivery, and it is never in this list.
        rows = (await session.execute(
            select(FollowupIntent).where(
                FollowupIntent.status.in_(("created", "sending", "cancelled")))
        )).scalars().all()
        pending = [(r.id, r.org_id, r.dependent_id, r.dependency_id, r.reason, r.text,
                    r.task_id, r.agent_id) for r in rows]

    for intent_id, org_id, dependent_id, dependency_id, reason, text, _task_id, agent_id in pending:
        async with SessionLocal() as session:
            dependent = await session.get(Task, dependent_id)
            dependency = await session.get(Task, dependency_id) if dependency_id else None
            row = await session.get(FollowupIntent, intent_id)
            if row is None or row.status not in ("created", "sending", "cancelled"):
                continue
            owed, why = await _still_owed(row, dependent, dependency, session)
            if not owed:
                row.status = "cancelled"
                await session.commit()
                cancelled += 1
                log.info("follow-up %s cancelled: %s", intent_id, why)
                continue
            target = agent_id or dependent.assignee_id
        if not target:
            await cancel(intent_id)
            cancelled += 1
            continue
        outcome = await deliver_one(org_id, dependency_id, dependent_id, reason, [target], text,
                                    notify=notify, reissue_cancelled=True)
        if outcome == "sent":
            sent += 1
        elif outcome in ("claimed-by-other", "already-sent", "adopted-existing", "in-flight",
                         "released-stale"):
            # released-stale counts as skipped, not sent: nothing went out this round; the row is
            # simply available again, and the next pass will try.
            skipped += 1
        elif outcome == "send-failed":
            failed += 1
        elif outcome == "cancelled":
            cancelled += 1

    if sent or cancelled or failed or skipped:
        log.info("follow-up sweep: sent=%d cancelled=%d failed=%d skipped=%d",
                 sent, cancelled, failed, skipped)
    return {"sent": sent, "cancelled": cancelled, "failed": failed, "skipped": skipped}


async def owed_count(org_id: str | None = None) -> int:
    """How many wake-ups are still owed. Makes the gap visible instead of silent."""
    async with SessionLocal() as session:
        stmt = select(FollowupIntent).where(FollowupIntent.status.in_(("created", "sending")))
        if org_id:
            stmt = stmt.where(FollowupIntent.org_id == org_id)
        return len((await session.execute(stmt)).scalars().all())
