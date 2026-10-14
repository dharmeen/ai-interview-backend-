"""add follow-up questions

Revision ID: 9d4f2b8e6a31
Revises: 7c1e9a3f5b20
Create Date: 2026-10-14 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d4f2b8e6a31'
down_revision: Union[str, Sequence[str], None] = '7c1e9a3f5b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('questions', sa.Column('sub_no', sa.Integer(), server_default='0', nullable=False))
    op.add_column('interviews', sa.Column('current_sub_no', sa.Integer(), server_default='0', nullable=False))
    # a follow-up shares its main question's order_no, so the position is (order_no, sub_no)
    op.drop_constraint('questions_interview_id_order_no_key', 'questions', type_='unique')
    op.create_unique_constraint(
        'questions_interview_id_order_no_sub_no_key', 'questions', ['interview_id', 'order_no', 'sub_no']
    )


def downgrade() -> None:
    """Downgrade schema."""
    # follow-ups would break the old (interview_id, order_no) constraint
    op.execute("DELETE FROM questions WHERE sub_no > 0")
    op.drop_constraint('questions_interview_id_order_no_sub_no_key', 'questions', type_='unique')
    op.create_unique_constraint('questions_interview_id_order_no_key', 'questions', ['interview_id', 'order_no'])
    op.drop_column('interviews', 'current_sub_no')
    op.drop_column('questions', 'sub_no')
