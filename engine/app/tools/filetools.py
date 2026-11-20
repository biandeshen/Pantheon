"""check_files: reopen deliverables the way their reader will (services/filecheck.py)."""

from __future__ import annotations

import asyncio
import hashlib

from app.services import filecheck
from app.tools.base import S, ToolContext, ToolError, arr, obj, resolve_in_workspace, tool

AMBIENT = ("check_files", "file_digest")  # for whoever writes files (resolve.py)


@tool("check_files", "Reopen files you made the way their reader will and see what it sees: "
      "sheets and filled cells of a workbook, formula cells with no saved value (blank to "
      "pandas and graders), error cells, CSV rows with the wrong number of fields, placeholder "
      "text, links to missing files, files that don't open. Without paths: every document, "
      "sheet, CSV, JSON, Markdown and HTML file in your workdir, newest first.",
      obj({"paths": {**arr(S), "description": "Files or folders (default: your workdir)."}}),
      category="workspace")
async def check_files(args: dict, ctx: ToolContext) -> str:
    root = ctx.workdir
    paths = []
    for rel in args.get("paths") or []:
        p = resolve_in_workspace(ctx, str(rel))
        if p.is_dir():
            paths += filecheck._walk(p)
        elif p.is_file():
            paths.append(p)
        else:
            raise ToolError(f"{rel} doesn't exist")
    if not args.get("paths"):
        paths = filecheck._walk(root)
    if not paths:
        return "No documents, sheets or data files to check."
    results = await asyncio.to_thread(filecheck.check_paths, root, paths)
    bad = sum(1 for r in results if r.problems)
    more = (f"\n(Checked the newest {filecheck.MAX_FILES} of {len(paths)}; name paths for the "
            "rest.)" if len(paths) > filecheck.MAX_FILES else "")
    return (f"{len(results)} file{'s' * (len(results) != 1)} checked, "
            f"{bad or 'none'} with problems.\n" + filecheck.report(results) + more)


@tool("file_digest", "The SHA-256 (and byte size) of a file, computed by the engine, not by you. "
      "Use this whenever a verdict, a review or an acceptance has to name a version: a hash you "
      "typed from memory or worked out yourself is not evidence, and a byte count is not a hash. "
      "The output is one line per file, `sha256  bytes  path`, so it can be quoted verbatim into "
      "a review and re-run by anyone to get the same string.",
      obj({"paths": {**arr(S), "description": "Files to digest (default: every file you are "
                                             "pointed at by name; no walk, so pass paths)."}}),
      category="workspace")
async def file_digest(args: dict, ctx: ToolContext) -> str:
    """Deterministic digests, because a model-computed hash is not evidence.

    Measured need: T-49's non-author verdict named the artifact and its byte count but not its
    sha256, and the acceptance clause says the verdict binds the hash. The candidate fix at the
    time was for the reviewer to run `node -e` and compute it -- which needs an approval, and
    which produces a number nobody can attribute to the engine rather than to the model. Having
    the engine hash the file removes both problems: no shell, no approval, and the digest lands
    in tool_calls where it can be replayed.
    """
    rels = args.get("paths") or []
    if not rels:
        raise ToolError("name at least one file: digests are per file, and there is no walk")
    lines: list[str] = []
    for rel in rels:
        p = resolve_in_workspace(ctx, str(rel))
        if p.is_dir():
            raise ToolError(f"{rel} is a folder; name the files (a digest of a folder has no "
                            "single meaning)")
        if not p.is_file():
            raise ToolError(f"{rel} doesn't exist")
        data = await asyncio.to_thread(p.read_bytes)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {len(data)}  {rel}")
    return "\n".join(lines)
