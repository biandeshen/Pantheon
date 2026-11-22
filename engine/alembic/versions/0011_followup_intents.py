"""A follow-up notice that survives the gap between "the close is saved" and "the notice exists"

Measured loss, in production: T-52 was closed at 2026-11-20T15:27:27 and T-53 -- the task whose only
dependency was T-52 -- has received nothing since. Its single message predates the close by thirteen
hours. So a downstream task was left waiting forever by a crash (or a bad edit) in the window between
committing the status change and sending the follow-up.

The window is real because the two operations are separate: `tasks.update` commits the status first,
then sends follow-ups through `comms.notify`, which opens its own session and commits again. Anything
that stops the process in between -- or any code path that closes a task without going through
`update` at all -- leaves a closed dependency and no wake-up, and nothing ever re-derives it, because
the "a dependency just closed" event only fires once.

So the intent is written down BEFORE it is sent, and a sweep finishes what a crash interrupted:

  created    the follow-up is owed
  sent       it went out (message_id points at it)
  cancelled  the reason disappeared (the dependent was closed/started meanwhile)

The unique key (dependency, dependent, reason) is the "exactly once" part: a task closed twice, or a
sweep racing a live send, cannot produce two notices for the same dependency closing.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-22 23:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'followup_intents',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('org_id', sa.String(), sa.ForeignKey('orgs.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('dependency_id', sa.String(), nullable=False, index=True),
        sa.Column('dependent_id', sa.String(), nullable=False, index=True),
        sa.Column('agent_id', sa.String(), nullable=True),
        sa.Column('reason', sa.String(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='created'),
        sa.Column('message_id', sa.String(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('dependency_id', 'dependent_id', 'reason',
                            name='uq_followup_intent'),
    )


def downgrade() -> None:
    op.drop_table('followup_intents')
