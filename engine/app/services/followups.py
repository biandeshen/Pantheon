"""Follow-ups that survive a crash between "the close is saved" and "the notice is sent".

The bug, measured in production rather than simulated: T-52 closed at 2026-11-20T15:27:27, and T-53 --
whose only dependency was T-52 -- has received nothing since (its single message predates the close by
thirteen hours). The two operations live in different transactions, so anything that stops the process
between them, or any close that bypasses tasks.update, leaves a closed dependency with no wake-up, and
nothing ever re-derives it: "a dependency just closed" only fires once.

Two halves, both needed:

  record_followups()   writes the intent to `followup_intents` BEFORE sending. Unique on
                       (dependency, dependent, reason), so closing a task twice -- or a sweep racing a
                       live send -- cannot produce two notices for the same event.
  sweep_followups()    finishes what a crash interrupted: any intent still `created` is sent now, and
                       an intent whose reason has disappeared (dependent closed, dependency reopened)
                       is cancelled rather than delivered as a stale instruction.

Why not simply wrap both in one transaction? Because `comms.notify` delivers and wakes agents -- it is
not a database write, and it can partially succeed. A durable intent that is *then* sent is the only
shape where "sent exactly once" is checkable afterwards.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.db import SessionLocal
from app.core.ids import utcnow
from app.models import FollowupIntent, Task

log = logging.getLogger(__name__)

CLOSED = ("done", "cancelled")


async def record_followups(org_id: str, intents: list[dict]) -> list[FollowupIntent]:
    """Persist what we owe, before trying to send it. Returns the rows actually created."""
    made: list[FollowupIntent] = []
    if not intents:
        return made
    # One transaction, one savepoint per row. A full rollback after an IntegrityError would discard
    # the rows already created in the same call, and re-opening transactions per row fights the
    # engine's BEGIN IMMEDIATE write lock (measured: "database is locked" under the test dispatcher).
    async with SessionLocal() as session:
        for it in intents:
            sp = await session.begin_nested()
            row = FollowupIntent(
                org_id=org_id, dependency_id=it["dependency_id"],
                dependent_id=it["dependent_id"], agent_id=it.get("agent_id"),
                reason=it["reason"], text=it["text"], task_id=it.get("task_id"),
                status="created",
            )
            session.add(row)
            try:
                await session.flush()
                await sp.commit()
            except IntegrityError:
                # Already owed (or already sent): the unique key is the exactly-once guarantee, and
                # hitting it is a normal outcome, not an error.
                await sp.rollback()
                continue
            made.append(row)
        await session.commit()
    return made


async def mark_sent(intent_id: str, message_id: str | None) -> None:
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent_id)
        if row is None:
            return
        row.status = "sent"
        row.message_id = message_id
        row.sent_at = utcnow()
        row.attempts = (row.attempts or 0) + 1
        await session.commit()


async def cancel(intent_id: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(FollowupIntent, intent_id)
        if row is None or row.status != "created":
            return
        row.status = "cancelled"
        await session.commit()


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
    if row.reason == "child-stop" and dependent.status not in CLOSED and dependent.assignee_id is None:
        return False, "child has no assignee"
    return True, ""


async def sweep_followups(notify=None) -> dict[str, int]:
    """Send what is owed, cancel what no longer applies. Returns counts, for the log and the tests."""
    from app.services import comms  # imported here: comms imports models, avoid a cycle at import time

    send = notify or comms.notify
    sent = cancelled = failed = 0
    # Plain tuples, session closed before sending anything: holding an ORM session open across the
    # sends would keep the write lock while comms.notify opens its own session (a real deadlock).
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(FollowupIntent).where(FollowupIntent.status == "created")
        )).scalars().all()
        pending = [(r.id, r.org_id, r.dependent_id, r.dependency_id, r.reason, r.text,
                    r.task_id, r.agent_id) for r in rows]
        await session.close()

    for intent_id, org_id, dependent_id, dependency_id, reason, text, task_id, agent_id in pending:
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
        try:
            msg = await send(org_id, [target], text, task_id=task_id)
        except Exception:  # noqa: BLE001 - one failed notice must not stop the others
            log.exception("follow-up %s could not be sent; it stays owed", intent_id)
            failed += 1
            continue
        await mark_sent(intent_id, getattr(msg, "id", None))
        sent += 1

    if sent or cancelled or failed:
        log.info("follow-up sweep: sent=%d cancelled=%d failed=%d", sent, cancelled, failed)
    return {"sent": sent, "cancelled": cancelled, "failed": failed}


async def owed_count(org_id: str | None = None) -> int:
    """How many wake-ups are still owed. Used by the page/health checks to make the gap visible."""
    async with SessionLocal() as session:
        stmt = select(FollowupIntent).where(FollowupIntent.status == "created")
        if org_id:
            stmt = stmt.where(FollowupIntent.org_id == org_id)
        return len((await session.execute(stmt)).scalars().all())
