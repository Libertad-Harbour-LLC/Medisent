"""Одобрения в базе, уникальность пары заявка+поставщик, кэш-телеметрия.

По результатам аудита docs/AUDIT.md, пункты 2, 3, 4, 8 и мелочь про кэш.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("quote_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_approvals_request_id_requests")
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["suppliers.id"], name=op.f("fk_approvals_supplier_id_suppliers")
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"], ["quote_requests.id"], name=op.f("fk_approvals_quote_id_quote_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index("ix_approvals_request_id", "approvals", ["request_id"])
    op.create_index("ix_approvals_pending", "approvals", ["kind", "decision"])

    # Дубликаты, если они успели появиться до этой миграции, оставляем: удалять
    # чужие записи молча нельзя. Индекс создаётся только на чистой таблице,
    # иначе миграция упадёт и это правильно — разобраться должен человек.
    op.create_unique_constraint(
        "quote_requests_request_supplier_uq", "quote_requests", ["request_id", "supplier_id"]
    )

    op.add_column("api_calls", sa.Column("cached_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_calls", "cached_tokens")
    op.drop_constraint("quote_requests_request_supplier_uq", "quote_requests", type_="unique")
    op.drop_index("ix_approvals_pending", table_name="approvals")
    op.drop_index("ix_approvals_request_id", table_name="approvals")
    op.drop_table("approvals")
