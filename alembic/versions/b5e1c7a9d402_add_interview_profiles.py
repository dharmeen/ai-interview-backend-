"""add interview profiles, difficulty rounds and question history

Revision ID: b5e1c7a9d402
Revises: 9d4f2b8e6a31
Create Date: 2026-10-14 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5e1c7a9d402'
down_revision: Union[str, Sequence[str], None] = '9d4f2b8e6a31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('interview_profiles',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=100), nullable=False),
    sa.Column('level', sa.Enum('fresher', 'junior', 'mid', 'senior', name='interviewlevel', native_enum=False, length=20), nullable=False),
    sa.Column('experience_years', sa.Integer(), nullable=False),
    sa.Column('interview_type', sa.Enum('technical', 'hr', 'mixed', name='interviewtype', native_enum=False, length=20), nullable=False),
    sa.Column('num_questions', sa.Integer(), nullable=False),
    sa.Column('job_description', sa.Text(), nullable=True),
    sa.Column('resume_text', sa.Text(), nullable=False),
    sa.Column('resume_filename', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_interview_profiles_user_id'), 'interview_profiles', ['user_id'], unique=False)

    op.add_column('interviews', sa.Column('profile_id', sa.Uuid(), nullable=True))
    op.add_column('interviews', sa.Column('difficulty_round', sa.Integer(), server_default='1', nullable=False))
    op.add_column('interviews', sa.Column('previous_questions', sa.JSON(), nullable=True))
    op.create_index(op.f('ix_interviews_profile_id'), 'interviews', ['profile_id'], unique=False)
    op.create_foreign_key(
        'interviews_profile_id_fkey', 'interviews', 'interview_profiles', ['profile_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('interviews_profile_id_fkey', 'interviews', type_='foreignkey')
    op.drop_index(op.f('ix_interviews_profile_id'), table_name='interviews')
    op.drop_column('interviews', 'previous_questions')
    op.drop_column('interviews', 'difficulty_round')
    op.drop_column('interviews', 'profile_id')
    op.drop_index(op.f('ix_interview_profiles_user_id'), table_name='interview_profiles')
    op.drop_table('interview_profiles')
