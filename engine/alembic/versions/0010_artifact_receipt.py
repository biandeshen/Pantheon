"""artifacts: record the Run and the exact bytes, so a verdict can bind a version

Why this migration exists, in the chairman's words: "验收读当次 Run 明确记录并可回查的产物版本，
当前 HEAD 另列；归属无证据就标缺，不靠时间推定."

The table had `task_id` all along and nothing ever wrote it -- 326 rows, 0 with a task, because
``write_file`` recorded org+path only. The visible consequence: P0-3 could not name the version a
Run produced, and the first fix (``git rev-list --before=<endedAt>``) credited a later commit to an
earlier run. That is a plumbing gap, not a product judgement, so it is fixed at the writer.

What is added, and what each field is allowed to mean:

  turn_id       the Run that wrote this version. NULL on old rows: never back-filled, because the
                only way to fill it would be a timestamp guess -- the exact inference being removed.
  sha256        the engine hashed the bytes it wrote. NULL on old rows, and on files too large to
                hash (the writer says why).
  source_sha    the git commit the file sat on when written; NULL when not in a git checkout.
  source_dirty  whether the file still differed from that commit. NULL means "no repo", not "clean".

Rows are versions now: the same path written twice at different content is two rows, so "which
version did this Run produce" stays answerable. Re-writing identical bytes stays one row (unique on
org+path+sha256), which keeps the file list from filling with copies of the same file.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-22 03:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('artifacts') as batch:
        batch.add_column(sa.Column('turn_id', sa.String(), nullable=True))
        batch.add_column(sa.Column('sha256', sa.String(), nullable=True))
        batch.add_column(sa.Column('source_sha', sa.String(), nullable=True))
        batch.add_column(sa.Column('source_dirty', sa.Boolean(), nullable=True))
        batch.create_index('ix_artifacts_turn_id', ['turn_id'])
        batch.create_index('ix_artifacts_task_id', ['task_id'])
        batch.create_unique_constraint('uq_artifact_version', ['org_id', 'path', 'sha256'])


def downgrade() -> None:
    with op.batch_alter_table('artifacts') as batch:
        batch.drop_constraint('uq_artifact_version', type_='unique')
        batch.drop_index('ix_artifacts_task_id')
        batch.drop_index('ix_artifacts_turn_id')
        batch.drop_column('source_dirty')
        batch.drop_column('source_sha')
        batch.drop_column('sha256')
        batch.drop_column('turn_id')
