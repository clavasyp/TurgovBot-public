"""Persist law-enrichment context and title ownership.

Revision ID: 026_law_enrichment_context
Revises: 025_assistant_case_digest
Create Date: 2026-07-29

``enrich_hash`` proves only that the article bytes were enriched.  Generated
synonyms and titles also depend on document/section/chapter context and on
exact-body group topology, which may change while those bytes stay identical.
The context hash lets the normal autonomous updater detect that drift.

Historical non-null titles cannot safely be guessed as parser- or
model-owned.  A NULL provenance on such a pre-existing title is deliberately
the fail-closed, unattested legacy state.  The versioned application
attestation path later classifies it as ``source``, ``generated`` or
``legacy_unknown`` from exact parser replay or durable artifact evidence.

Do not backfill that classification in this migration.  PostgreSQL keeps the
ACCESS EXCLUSIVE lock acquired by ``ADD COLUMN`` until the Alembic transaction
commits; a table-wide UPDATE here would therefore turn a metadata-only schema
change into extended downtime on a large catalog.  The NOT VALID checks still
protect every new or changed row immediately.  Existing rows can only contain
NULL in the newly added columns and therefore already satisfy both checks;
leaving validation to a later online schema-maintenance revision avoids a
redundant full-table scan while the DDL lock is held.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "026_law_enrichment_context"
down_revision: Union[str, None] = "025_assistant_case_digest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "law_articles",
        sa.Column("title_provenance", sa.Text(), nullable=True),
    )
    op.add_column(
        "law_articles",
        sa.Column(
            "enrichment_context_hash",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "law_articles",
        sa.Column(
            "enrichment_output_hash",
            sa.String(length=64),
            nullable=True,
        ),
    )
    # Never infer historical ownership from title/enrich_hash here.  NULL on a
    # pre-existing non-null title is intentionally treated by the application
    # as unattested legacy state until the versioned attestation workflow runs.
    op.create_check_constraint(
        "ck_law_articles_title_provenance",
        "law_articles",
        "title_provenance IS NULL OR "
        "title_provenance IN ('source', 'generated', 'legacy_unknown')",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        "ck_law_articles_enrichment_context_hash",
        "law_articles",
        "enrichment_context_hash IS NULL OR "
        "enrichment_context_hash ~ '^[0-9a-f]{64}$'",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        "ck_law_articles_enrichment_output_hash",
        "law_articles",
        "enrichment_output_hash IS NULL OR "
        "enrichment_output_hash ~ '^[0-9a-f]{64}$'",
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_law_articles_enrichment_output_hash",
        "law_articles",
        type_="check",
    )
    op.drop_constraint(
        "ck_law_articles_enrichment_context_hash",
        "law_articles",
        type_="check",
    )
    op.drop_constraint(
        "ck_law_articles_title_provenance",
        "law_articles",
        type_="check",
    )
    op.drop_column("law_articles", "enrichment_output_hash")
    op.drop_column("law_articles", "enrichment_context_hash")
    op.drop_column("law_articles", "title_provenance")
