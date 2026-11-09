"""Rolling digest ("карта дела") for long assistant cases.

Дела ролевого помощника доходят до 432 000 символов при 135 сообщениях, и вся история
переотправлялась модели каждым ходом. Грубая обрезка по лимиту (миграция не нужна, уже
в проде) спасает от переполнения, но выбрасывает середину дела безвозвратно: эпизоды,
имена, даты и уже принятые решения исчезают из контекста.

Здесь хранится сжатая карта дела: она покрывает всё, что старше свежего хвоста диалога,
и обновляется инкрементально (предыдущая карта + новые сообщения), поэтому старые факты
переносятся дальше, а не теряются.

Revision ID: 025_assistant_case_digest
Revises: 024_law_synonym_backfill_log
Create Date: 2026-07-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "025_assistant_case_digest"
down_revision: Union[str, None] = "024_law_synonym_backfill_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_case_digests",
        # Одна карта на дело: обновляется на месте, история версий не нужна.
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        # ID последнего сообщения, вошедшего в карту. Всё, что новее, подаётся модели сырым.
        sa.Column("covered_message_id", sa.Integer(), nullable=False),
        sa.Column("covered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["case_id"], ["assistant_cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("case_id"),
        if_not_exists=True,
    )


def downgrade() -> None:
    # Карта — производный кэш: удаление ничего не теряет, сообщения дела лежат отдельно.
    op.drop_table("assistant_case_digests", if_exists=True)
