"""Add versioned FRIDA embeddings and a durable embedding job queue.

Revision ID: 020_add_law_embedding_versioning
Revises: 019_add_universal_license
Create Date: 2026-07-15

The legacy ``law_articles.embedding_local`` column remains in place as a
rollback/compatibility copy.  Historical rows did not record their exact
input fingerprint, so upgrade deliberately does not certify or copy them into
``law_article_embeddings``.  Every article is queued for a fresh FRIDA-v1
backfill instead.  New code should use ``law_article_embeddings`` and always
select an explicit ``space_id``.

``frida-v1`` identifies the exact intended production space:
ai-forever/FRIDA, 1536 dimensions, CLS pooling, L2 normalisation and the
search_query/search_document prefixes. Its full artifact-manifest SHA-256 is
recorded in ``law_embedding_spaces.model_revision``; the weights hash remains
in metadata for diagnostics. This passport describes newly computed vectors;
it is not evidence about historical rows in the legacy column.

The input fingerprint is SHA-256 over the UTF-8 bytes of:

    title-or-empty + U+001F + penalty_summary-or-empty
        + U+001F + full_text[:2000]

The vector-space passport separately versions the model and document recipe.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "020_add_law_embedding_versioning"
down_revision: Union[str, None] = "019_add_universal_license"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_SPACE_ID = "frida-v1"

# Keep this expression byte-for-byte equivalent to the Python fingerprint
# helper used by the catalog/embedding worker.
_FRIDA_V1_INPUT_HASH_SQL = """
encode(
    digest(
        convert_to(
            concat_ws(
                chr(31),
                COALESCE(title, ''),
                COALESCE(penalty_summary, ''),
                LEFT(COALESCE(full_text, ''), 2000)
            ),
            'UTF8'
        ),
        'sha256'
    ),
    'hex'
)
"""


def upgrade() -> None:
    # SHA-256 input fingerprints use pgcrypto. Do not remove the extension in
    # downgrade because other application objects may share it later.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # A vector-space passport prevents vectors produced by different model
    # snapshots, pooling rules, prefixes or input recipes from being mixed.
    # The active space intentionally remains an application config choice.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_embedding_spaces (
            space_id         TEXT PRIMARY KEY,
            model_name       TEXT NOT NULL,
            model_revision   TEXT NOT NULL,
            dimensions       INT NOT NULL,
            query_prefix     TEXT NOT NULL,
            document_prefix  TEXT NOT NULL,
            pooling          TEXT NOT NULL,
            normalized       BOOLEAN NOT NULL,
            recipe_version   TEXT NOT NULL,
            metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- law_article_embeddings.embedding is vector(1536); a different
            -- dimension therefore requires a schema migration, not just a
            -- new passport row.
            CONSTRAINT ck_law_embedding_spaces_dimensions CHECK (dimensions = 1536),
            CONSTRAINT ck_law_embedding_spaces_space_id CHECK (btrim(space_id) <> '')
        )
        """
    )
    op.execute(
        """
        INSERT INTO law_embedding_spaces
            (space_id, model_name, model_revision, dimensions,
             query_prefix, document_prefix, pooling, normalized,
             recipe_version, metadata)
        VALUES
            (
                'frida-v1',
                'ai-forever/FRIDA',
                'sha256:47b1442ef6e7f94b87f9b22a6d66a48b3468a432c1d080f0495d1d533c042084',
                1536,
                'search_query: ',
                'search_document: ',
                'cls',
                TRUE,
                'law-article-v1',
                jsonb_build_object(
                    'hash_algorithm', 'sha256',
                    'hash_separator', 'U+001F',
                    'input_fields', jsonb_build_array(
                        'title', 'penalty_summary', 'full_text_first_2000_chars'
                    ),
                    'embedding_text',
                        'non-empty fields joined by ". "; fallback "статья"',
                    'legacy_column', 'law_articles.embedding_local',
                    'weights_sha256',
                        'f48a05972031f3e9b4799b548a148215097f148332c8a68c3bf575497d6a0d43',
                    'artifact_manifest_sha256',
                        '47b1442ef6e7f94b87f9b22a6d66a48b3468a432c1d080f0495d1d533c042084',
                    'artifact_manifest_format',
                        'sorted relative path + NUL + bytes + NUL',
                    'artifact_manifest_files', jsonb_build_array(
                        '1_Pooling/config.json',
                        'config.json',
                        'config_sentence_transformers.json',
                        'merges.txt',
                        'model.safetensors',
                        'modules.json',
                        'sentence_bert_config.json',
                        'special_tokens_map.json',
                        'tokenizer_config.json',
                        'tokenizer.json',
                        'vocab.json'
                    ),
                    'runtime_versions', jsonb_build_object(
                        'sentence-transformers', '3.4.1',
                        'transformers', '4.57.6',
                        'tokenizers', '0.22.2',
                        'torch', '2.13.0+cpu',
                        'numpy', '2.4.6'
                    )
                )
            )
        ON CONFLICT (space_id) DO NOTHING
        """
    )

    # Passport metadata on the legacy column. All fields stay nullable because
    # historical vectors have no trustworthy saved input fingerprint. The
    # worker fills these fields only after a fresh, verified inference.
    op.execute(
        "ALTER TABLE law_articles "
        "ADD COLUMN IF NOT EXISTS embedding_local_space TEXT"
    )
    op.execute(
        "ALTER TABLE law_articles "
        "ADD COLUMN IF NOT EXISTS embedding_local_input_hash TEXT"
    )
    op.execute(
        "ALTER TABLE law_articles "
        "ADD COLUMN IF NOT EXISTS embedding_local_updated_at TIMESTAMPTZ"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'fk_law_articles_embedding_local_space'
                  AND conrelid = 'law_articles'::regclass
            ) THEN
                ALTER TABLE law_articles
                ADD CONSTRAINT fk_law_articles_embedding_local_space
                FOREIGN KEY (embedding_local_space)
                REFERENCES law_embedding_spaces(space_id);
            END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_articles_emb_local_space_server
        ON law_articles (server_id, embedding_local_space)
        WHERE embedding_local IS NOT NULL
        """
    )

    # Canonical multi-version storage. One article may retain one vector per
    # registered space, making a config switch and rollback non-destructive.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_article_embeddings (
            article_id  INT NOT NULL
                        REFERENCES law_articles(id) ON DELETE CASCADE,
            server_id   TEXT NOT NULL,
            space_id    TEXT NOT NULL
                        REFERENCES law_embedding_spaces(space_id),
            input_hash  TEXT NOT NULL,
            embedding   vector(1536) NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (article_id, space_id),
            CONSTRAINT ck_law_article_embeddings_input_hash
                CHECK (btrim(input_hash) <> '')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_article_embeddings_server_space
        ON law_article_embeddings (server_id, space_id, article_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_article_embeddings_hnsw
        ON law_article_embeddings USING hnsw (embedding vector_cosine_ops)
        """
    )
    # Durable queue shared by catalog writers (producers) and the API process
    # that owns the local model (consumer). Successful jobs are deleted; a
    # terminal failure remains as status='failed' for diagnostics/retry.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_embedding_jobs (
            id            BIGSERIAL PRIMARY KEY,
            article_id    INT NOT NULL
                          REFERENCES law_articles(id) ON DELETE CASCADE,
            server_id     TEXT NOT NULL,
            space_id      TEXT NOT NULL
                          REFERENCES law_embedding_spaces(space_id),
            input_hash    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            keep_current  BOOLEAN NOT NULL DEFAULT FALSE,
            attempts      INT NOT NULL DEFAULT 0,
            last_error    TEXT,
            available_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            locked_at     TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_law_embedding_jobs_article_space
                UNIQUE (article_id, space_id),
            CONSTRAINT ck_law_embedding_jobs_status
                CHECK (status IN ('pending', 'processing', 'failed')),
            CONSTRAINT ck_law_embedding_jobs_attempts
                CHECK (attempts >= 0),
            CONSTRAINT ck_law_embedding_jobs_input_hash
                CHECK (btrim(input_hash) <> '')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_embedding_jobs_claim
        ON law_embedding_jobs (space_id, available_at, id)
        WHERE status IN ('pending', 'failed')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_embedding_jobs_stale_processing
        ON law_embedding_jobs (locked_at, id)
        WHERE status = 'processing'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_embedding_jobs_server_space_status
        ON law_embedding_jobs (server_id, space_id, status)
        """
    )
    # A legacy vector cannot be proven to represent today's article input: the
    # old schema never persisted its input hash. Queue every article and keep
    # canonical storage empty until the worker performs verified inference.
    op.execute(
        f"""
        INSERT INTO law_embedding_jobs
            (article_id, server_id, space_id, input_hash,
             status, keep_current, attempts, available_at)
        SELECT id,
               server_id,
               '{_LEGACY_SPACE_ID}',
               {_FRIDA_V1_INPUT_HASH_SQL},
               'pending',
               FALSE,
               0,
               now()
        FROM law_articles
        WHERE kind = 'article'
        ON CONFLICT (article_id, space_id) DO NOTHING
        """
    )

    # Rebuilding the related-article matrix is also durable. A marker is kept
    # until rebuilding succeeds, so a process restart cannot silently lose it.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_related_rebuild_jobs (
            server_id     TEXT NOT NULL,
            space_id      TEXT NOT NULL
                          REFERENCES law_embedding_spaces(space_id),
            status        TEXT NOT NULL DEFAULT 'pending',
            attempts      INT NOT NULL DEFAULT 0,
            last_error    TEXT,
            available_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            locked_at     TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (server_id, space_id),
            CONSTRAINT ck_law_related_rebuild_jobs_status
                CHECK (status IN ('pending', 'processing', 'failed')),
            CONSTRAINT ck_law_related_rebuild_jobs_attempts
                CHECK (attempts >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_related_rebuild_jobs_claim
        ON law_related_rebuild_jobs (available_at, server_id, space_id)
        WHERE status IN ('pending', 'failed')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_related_rebuild_jobs_stale
        ON law_related_rebuild_jobs (locked_at, server_id, space_id)
        WHERE status = 'processing'
        """
    )

    # The old related cache has no provenance either: depending on when it was
    # last rebuilt it may represent a Gemini centroid, legacy FRIDA, or stale
    # article inputs. Never label those rows as verified frida-v1. It is derived
    # data, so clear it and keep a durable dirty marker hidden from new readers
    # until the fresh FRIDA corpus for that server has drained and rebuilt it.
    op.execute("DELETE FROM law_article_related")
    op.execute(
        f"""
        INSERT INTO law_related_rebuild_jobs
            (server_id, space_id, status, attempts, available_at)
        SELECT DISTINCT server_id, '{_LEGACY_SPACE_ID}', 'pending', 0, now()
        FROM law_articles
        WHERE kind = 'article'
        ON CONFLICT (server_id, space_id) DO NOTHING
        """
    )

    # Version the precomputed related matrix too. Deployment stops the old
    # catalog writer before this migration, so the primary key can include the
    # vector space immediately instead of making a future v2 collide with v1.
    op.execute(
        "ALTER TABLE law_article_related "
        "ADD COLUMN IF NOT EXISTS vector_space TEXT"
    )
    op.execute(
        f"""
        UPDATE law_article_related
        SET vector_space = '{_LEGACY_SPACE_ID}'
        WHERE vector_space IS NULL
        """
    )
    op.execute(
        f"ALTER TABLE law_article_related "
        f"ALTER COLUMN vector_space SET DEFAULT '{_LEGACY_SPACE_ID}'"
    )
    op.execute(
        "ALTER TABLE law_article_related "
        "ALTER COLUMN vector_space SET NOT NULL"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'fk_law_article_related_vector_space'
                  AND conrelid = 'law_article_related'::regclass
            ) THEN
                ALTER TABLE law_article_related
                ADD CONSTRAINT fk_law_article_related_vector_space
                FOREIGN KEY (vector_space)
                REFERENCES law_embedding_spaces(space_id);
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        DECLARE
            current_pk TEXT;
        BEGIN
            SELECT conname INTO current_pk
            FROM pg_constraint
            WHERE conrelid = 'law_article_related'::regclass
              AND contype = 'p';

            IF current_pk IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE law_article_related DROP CONSTRAINT %I',
                    current_pk
                );
            END IF;

            ALTER TABLE law_article_related
            ADD CONSTRAINT pk_law_article_related_space
            PRIMARY KEY (article_id, vector_space, related_id);
        END $$
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_article_related_lookup
        ON law_article_related (article_id, vector_space, score DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_article_related_related_id
        ON law_article_related (related_id)
        """
    )

    # law_documents and administrative cleanup paths can delete articles via
    # ON DELETE CASCADE without going through the catalog service.  Snapshot
    # every affected vector space before its embedding/related rows cascade,
    # and durably dirty the graph.  This makes direct and future delete paths
    # as safe as the normal catalog rebuild path.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION law_mark_related_dirty_on_article_delete()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO law_related_rebuild_jobs
                (server_id, space_id, status, attempts, last_error,
                 available_at, locked_at, created_at, updated_at)
            SELECT OLD.server_id, affected.space_id, 'pending', 0, NULL,
                   now(), NULL, now(), now()
            FROM (
                SELECT embedding.space_id
                FROM law_article_embeddings embedding
                WHERE embedding.article_id = OLD.id
                UNION
                SELECT job.space_id
                FROM law_embedding_jobs job
                WHERE job.article_id = OLD.id
                UNION
                SELECT related.vector_space
                FROM law_article_related related
                WHERE related.article_id = OLD.id OR related.related_id = OLD.id
            ) AS affected
            WHERE affected.space_id IS NOT NULL
            ON CONFLICT (server_id, space_id) DO UPDATE SET
                status = 'pending',
                attempts = 0,
                last_error = NULL,
                available_at = now(),
                locked_at = NULL,
                updated_at = now();
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_law_article_delete_marks_related_dirty
        ON law_articles
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_law_article_delete_marks_related_dirty
        BEFORE DELETE ON law_articles
        FOR EACH ROW
        EXECUTE FUNCTION law_mark_related_dirty_on_article_delete()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_law_article_delete_marks_related_dirty
        ON law_articles
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS law_mark_related_dirty_on_article_delete()"
    )

    # Preserve every verified FRIDA-v1 vector for the legacy application before
    # canonical storage and its passport metadata disappear.
    op.execute(
        f"""
        UPDATE law_articles AS article
        SET embedding_local = embedding.embedding
        FROM law_article_embeddings AS embedding
        WHERE embedding.article_id = article.id
          AND embedding.space_id = '{_LEGACY_SPACE_ID}'
        """
    )

    op.execute("DROP INDEX IF EXISTS idx_law_article_related_related_id")
    op.execute("DROP INDEX IF EXISTS idx_law_article_related_lookup")
    op.execute(
        f"DELETE FROM law_article_related "
        f"WHERE vector_space <> '{_LEGACY_SPACE_ID}'"
    )
    op.execute(
        "ALTER TABLE law_article_related "
        "DROP CONSTRAINT IF EXISTS pk_law_article_related_space"
    )
    op.execute(
        "ALTER TABLE law_article_related "
        "ADD CONSTRAINT law_article_related_pkey PRIMARY KEY (article_id, related_id)"
    )
    op.execute(
        "ALTER TABLE law_article_related "
        "DROP CONSTRAINT IF EXISTS fk_law_article_related_vector_space"
    )
    op.execute(
        "ALTER TABLE law_article_related "
        "DROP COLUMN IF EXISTS vector_space"
    )

    op.execute("DROP TABLE IF EXISTS law_related_rebuild_jobs")
    op.execute("DROP TABLE IF EXISTS law_embedding_jobs")
    op.execute("DROP TABLE IF EXISTS law_article_embeddings")

    op.execute("DROP INDEX IF EXISTS idx_law_articles_emb_local_space_server")
    op.execute(
        "ALTER TABLE law_articles "
        "DROP COLUMN IF EXISTS embedding_local_updated_at"
    )
    op.execute(
        "ALTER TABLE law_articles "
        "DROP COLUMN IF EXISTS embedding_local_input_hash"
    )
    op.execute(
        "ALTER TABLE law_articles "
        "DROP COLUMN IF EXISTS embedding_local_space"
    )

    op.execute("DROP TABLE IF EXISTS law_embedding_spaces")
