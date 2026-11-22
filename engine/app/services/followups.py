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


async def claim(intent_id: str) -> bool:
    """Take exclusive responsibility for sending one intent. False means someone else has it.

    A conditional UPDATE, not a read-then-write: two callers (live send and sweep, or two sweeps)
    both run this statement, and the database decides. Without it, "sent exactly once" held only
    because restarts happened to be rare.
    """
    async with SessionLocal() as session:
        res = await session.execute(
            update(FollowupIntent)
            .where(FollowupIntent.id == intent_id, FollowupIntent.status == "created")
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
    """Did this intent's notice already get created? Then never create a second one."""
    if not row.message_id:
        return None
    from app.models import Message

    async with SessionLocal() as session:
        return await session.get(Message, row.message_id)


async def deliver_one(org_id: str, dependency_id: str, dependent_id: str, reason: str,
                      targets: list[str], text: str, *, depth: int = 0, notify=None) -> str:
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
        if row.status == "cancelled":
            return "cancelled"

    if not await claim(intent_id):
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
        msg = await send(org_id, targets, text, depth=depth, task_id=dependent_id)
    except Exception:  # noqa: BLE001 - the caller must keep going; the row stays owed
        log.exception("follow-up %s could not be sent; releasing it", intent_id)
        await release(intent_id)
        return "send-failed"
    await mark_sent(intent_id, getattr(msg, "id", None))
    return "sent"


async def _row(intent_id: str) -> FollowupIntent | None:
    async with SessionLocal() as session:
        return await session.get(FollowupIntent, intent_id)


def _still_owed(row: FollowupIntent, dependent: Task | None, dependency: Task | None) -> tuple[bool, str]:
    """Is the reason still true? A stale follow-up is worse than none: it tells someone to start."""
    if dependent is None:
        return False, "dependent task is gone"
    if dependent.status in CLOSED:
        return False, "dependent is already closed"
    if row.reason == "dependencies-closed":
        if dependency is not None and dependency.status not in CLOSED:
            return False, "dependency was reopened"
        if not dependent.assignee_id:
            return False, "dependent has no assignee to wake"
    if row.reason == "child-stop" and dependent.assignee_id is None:
        return False, "child has no assignee"
    return True, ""


async def sweep_followups(notify=None) -> dict[str, int]:
    """Send what is owed, cancel what no longer applies. Counts go to the log and the tests."""
    sent = cancelled = failed = skipped = 0
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(FollowupIntent).where(FollowupIntent.status == "created")
        )).scalars().all()
        pending = [(r.id, r.org_id, r.dependent_id, r.dependency_id, r.reason, r.text,
                    r.task_id, r.agent_id) for r in rows]

    for intent_id, org_id, dependent_id, dependency_id, reason, text, _task_id, agent_id in pending:
        async with SessionLocal() as session:
            dependent = await session.get(Task, dependent_id)
            dependency = await session.get(Task, dependency_id) if dependency_id else None
            row = await session.get(FollowupIntent, intent_id)
            if row is None or row.status != "created":
                continue
            owed, why = _still_owed(row, dependent, dependency)
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
                                    notify=notify)
        if outcome == "sent":
            sent += 1
        elif outcome in ("claimed-by-other", "already-sent", "adopted-existing"):
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
