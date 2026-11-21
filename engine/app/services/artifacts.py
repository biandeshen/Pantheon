"""Artifacts: the versions agents wrote, each bound to the Run that wrote it.

The receipt has to answer "which version did THIS run produce" without a timestamp guess. So the
writer records, at write time and from the engine rather than from the model:

  task_id / turn_id   the task this Run was working on, and the Run
  sha256              of the bytes just written
  source_sha/dirty    the git commit the file sat on, and whether it differed

Everything here is read, never inferred. If the Run's inbox names no task, task_id stays NULL and
says so; a file with no task is a finding, not something to fill in later.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.events import bus
from app.models import Artifact, Message, Turn

if TYPE_CHECKING:
    from app.tools.base import ToolContext

# Files above this are indexed without a digest: hashing a 2 GB video on every write would stall
# the turn, and the receipt says "no hash" rather than a wrong one.
MAX_HASH_BYTES = 32 * 1024 * 1024


def artifact_to_dict(a: Artifact) -> dict[str, Any]:
    return {"id": a.id, "orgId": a.org_id, "agentId": a.agent_id, "taskId": a.task_id,
            "turnId": a.turn_id, "path": a.path, "title": a.title, "mime": a.mime,
            "size": a.size, "sha256": a.sha256, "sourceSha": a.source_sha,
            "sourceDirty": a.source_dirty,
            "createdAt": a.created_at.isoformat() if a.created_at else None}


async def task_of_turn(turn_id: str | None) -> str | None:
    """The task a Run was working on: the task its trigger messages name.

    Read from ``messages.task_id``, which the engine sets when it delivers task mail. A turn whose
    inbox names no task (a plain chat, a flow step) returns None -- and the receipt then says the
    task is unknown instead of borrowing the agent's most recent one.
    """
    if not turn_id:
        return None
    async with SessionLocal() as session:
        turn = await session.get(Turn, turn_id)
        ids = list((turn.trigger_message_ids if turn else None) or [])
        if not ids:
            return None
        rows = (await session.execute(select(Message.task_id).where(
            Message.id.in_(ids), Message.task_id.is_not(None)))).scalars().all()
    named = [t for t in rows if t]
    # One task, or none: a turn that mixes tasks must not be pinned to an arbitrary one.
    return named[0] if len(set(named)) == 1 else None


def _git_source(path: Path) -> tuple[str | None, bool | None]:
    """The commit the file sits on, and whether it differs from it. (None, None) outside git."""
    def run(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=str(path.parent), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=10,
                           check=False)
        return (r.stdout or "").strip() if r.returncode == 0 else ""

    try:
        sha = run("rev-parse", "HEAD")
        if not sha:
            return None, None
        # --error-unmatch: a file git doesn't track is dirty, not "unchanged".
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", path.name],
                                 cwd=str(path.parent), capture_output=True, text=True,
                                 timeout=10, check=False).returncode == 0
        dirty = True if not tracked else bool(run("status", "--porcelain", "--", path.name))
        return sha, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def _digest(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_HASH_BYTES:
            return None
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


async def record(ctx: ToolContext, path: Path) -> Artifact:
    rel = str(path.relative_to(ctx.workspace)).replace("\\", "/")
    mime = mimetypes.guess_type(path.name)[0] or "text/plain"
    size = path.stat().st_size
    sha256, source = await asyncio.gather(
        asyncio.to_thread(_digest, path), asyncio.to_thread(_git_source, path))
    source_sha, source_dirty = source
    task_id = await task_of_turn(ctx.turn_id)
    async with SessionLocal() as session:
        # A version, not a path: same bytes at the same path is the same row; different bytes is
        # a new one, so "the version this Run produced" stays answerable after a later rewrite.
        art = (await session.execute(select(Artifact).where(
            Artifact.org_id == ctx.org.id, Artifact.path == rel,
            Artifact.sha256 == sha256))).scalar_one_or_none() if sha256 else None
        if art is None:
            art = Artifact(org_id=ctx.org.id, agent_id=ctx.agent.id, task_id=task_id,
                           turn_id=ctx.turn_id, path=rel, title=path.name, mime=mime,
                           size=size, sha256=sha256, source_sha=source_sha,
                           source_dirty=source_dirty)
            session.add(art)
        else:
            # Rewritten by the same Run with the same bytes: refresh the attribution, keep the id.
            art.agent_id, art.size, art.mime = ctx.agent.id, size, mime
            art.task_id, art.turn_id = task_id or art.task_id, ctx.turn_id or art.turn_id
            art.source_sha, art.source_dirty = source_sha, source_dirty
        await session.commit()
    await bus.publish("artifact.updated", artifact_to_dict(art), org_id=ctx.org.id,
                      agent_id=ctx.agent.id)
    return art
