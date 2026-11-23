"""artifacts: the uniqueness is per Run, so two Runs writing identical bytes both keep their receipt

Measured bug: `uq_artifact_version` was (org_id, path, sha256). When a later Run rewrote a file with
byte-identical content, `record()` found that row and updated its `turn_id` to the new Run -- which
moved the receipt out from under the earlier Run. `/api/v1/turns/{earlier}` then returned an empty
artifact list, so the earlier Run's P0-3 verdict flipped to "recorded nothing" even though nothing
about its work had changed.

Both Runs did write those bytes; the honest model is one row each. So the key becomes
(org_id, path, sha256, turn_id):

  * the same Run writing the same bytes twice still collapses to one row (its own retry), which is
    what the key was for
  * a different Run writing the same bytes gets its own row
  * nothing is back-filled: rows written before this migration keep the turn they had

SQLite cannot alter a unique constraint in place, so the table is rebuilt with batch_alter_table,
which copies every row across unchanged.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-23 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('artifacts', recreate='always') as batch:
        batch.drop_constraint('uq_artifact_version', type_='unique')
        batch.create_unique_constraint('uq_artifact_version',
                                      ['org_id', 'path', 'sha256', 'turn_id'])


def downgrade() -> None:
    # Going back can collide: two Runs on one path+hash now exist as two rows and the old key allows
    # one. Refuse loudly rather than silently deleting somebody's receipt.
    conn = op.get_bind()
    dup = conn.execute(sa.text(
        "SELECT org_id, path, sha256, COUNT(*) c FROM artifacts "
        "WHERE sha256 IS NOT NULL GROUP BY org_id, path, sha256 HAVING c > 1 LIMIT 1"
    )).fetchone()
    if dup is not None:
        raise RuntimeError(
            "cannot downgrade: two Runs already share one path+sha256 "
            f"({dup[1]} {str(dup[2])[:12]}…). Deleting a receipt to satisfy the old key would hide "
            "which Run produced which version. Resolve by hand first."
        )
    with op.batch_alter_table('artifacts', recreate='always') as batch:
        batch.drop_constraint('uq_artifact_version', type_='unique')
        batch.create_unique_constraint('uq_artifact_version', ['org_id', 'path', 'sha256'])
