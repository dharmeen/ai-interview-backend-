"""add resume to interviews

Revision ID: 7c1e9a3f5b20
Revises: 151ea85ec93d
Create Date: 2026-10-14 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c1e9a3f5b20'
down_revision: Union[str, Sequence[str], None] = '151ea85ec93d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # nullable: interviews created before resumes were required have none
    op.add_column('interviews', sa.Column('resume_text', sa.Text(), nullable=True))
    op.add_column('interviews', sa.Column('resume_filename', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('interviews', 'resume_filename')
    op.drop_column('interviews', 'resume_text')
