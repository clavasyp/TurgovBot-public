"""Add append-only evidence for synonym backfills and enrichment replay.

Revision ID: 024_law_synonym_backfill_log
Revises: 023_law_document_sync_pending
Create Date: 2026-07-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "024_law_synonym_backfill_log"
down_revision: Union[str, None] = "023_law_document_sync_pending"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "law_synonym_backfill_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$'",
            name="ck_law_synonym_backfill_runs_plan_hash",
        ),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_law_synonym_backfill_runs_manifest_hash",
        ),
    )
    op.create_table(
        "law_synonym_backfill_inserts",
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        # Deliberately no FK: this is immutable audit evidence.  A cascade
        # would erase proof of a missing row, while a restrictive FK would
        # block normal catalog/article lifecycle operations.
        sa.Column("synonym_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("article_hash", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("weight", sa.REAL(), nullable=False),
        sa.Column("suggested_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("run_id", "synonym_id"),
    )
    op.create_table(
        "law_enrichment_artifact_commits",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("server_id", sa.Text(), nullable=False),
        # Deliberately no FK/CASCADE: a later catalog rebuild must not erase
        # the durable evidence used to distinguish an immediate crash replay
        # from a genuinely changed enrichment state.
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("article_hash", sa.Text(), nullable=False),
        sa.Column(
            "generation_input_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("artifact_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "post_commit_generation_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "server_id",
            "article_id",
            "article_hash",
            "artifact_hash",
            "post_commit_generation_hash",
            name="uq_law_enrich_commit_artifact_outcome",
        ),
        sa.CheckConstraint(
            "generation_input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_law_enrich_commit_input_hash",
        ),
        sa.CheckConstraint(
            "artifact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_law_enrich_commit_artifact_hash",
        ),
        sa.CheckConstraint(
            "post_commit_generation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_law_enrich_commit_post_hash",
        ),
    )
    # Candidate collision checks run for small generated batches.  This
    # expression index lets PostgreSQL probe the exact runtime signature
    # instead of evaluating candidates x every synonym on the server.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_synonyms_server_exact_signature
        ON law_synonyms (
          server_id,
          (tsvector_to_array(to_tsvector('russian', term))),
          ((to_tsvector('simple', term)
              @@ plainto_tsquery('simple', 'без'))),
          ((to_tsvector('simple', term)
              @@ plainto_tsquery('simple', 'не'))),
          ((to_tsvector('simple', term)
              @@ phraseto_tsquery('simple', 'без цели'))),
          ((to_tsvector('simple', term)
              @@ phraseto_tsquery('simple', 'с целью')))
        )
        INCLUDE (article_id, term)
        WHERE status = 'approved'
          AND char_length(term) >= 4
          AND cardinality(
                tsvector_to_array(to_tsvector('russian', term))
              ) > 0
        """
    )
    op.execute("ANALYZE law_synonyms")


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS idx_law_synonyms_server_exact_signature"
    )
    op.drop_table("law_enrichment_artifact_commits")
    op.drop_table("law_synonym_backfill_inserts")
    op.drop_table("law_synonym_backfill_runs")
