"""Add a durable marker for unfinished law-document derived sync.

Revision ID: 023_law_document_sync_pending
Revises: 022_add_law_search_events
Create Date: 2026-07-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "023_law_document_sync_pending"
down_revision: Union[str, None] = "022_add_law_search_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "law_documents",
        sa.Column(
            "derived_sync_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "idx_law_documents_derived_sync_pending",
        "law_documents",
        ["server_id", "source_thread_id"],
        unique=False,
        postgresql_where=sa.text("derived_sync_pending"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_law_documents_derived_sync_pending",
        table_name="law_documents",
    )
    op.drop_column("law_documents", "derived_sync_pending")
