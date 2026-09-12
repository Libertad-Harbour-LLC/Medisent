"""Начальная схема: восемь таблиц ТЗ плюс три служебные.

Revision ID: 0001
Revises:
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pg_trgm нужен для склейки критериев по смыслу (этап 6). На managed-базе
    # прав на CREATE EXTENSION может не быть — тогда работаем без триграмм,
    # склейка останется на точном совпадении. Миграция от этого не падает.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
                BEGIN
                    CREATE EXTENSION pg_trgm;
                EXCEPTION WHEN insufficient_privilege OR feature_not_supported THEN
                    RAISE NOTICE 'pg_trgm недоступен — склейка критериев только по точному совпадению';
                END;
            END IF;
        END $$;
        """
    )

    op.create_table(
        "requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("raw_input", sa.Text(), nullable=True),
        sa.Column("input_kind", sa.Text(), nullable=True),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requests")),
        sa.UniqueConstraint("token", name=op.f("uq_requests_token")),
    )

    op.create_table(
        "suppliers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("tax_id", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("found_via", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppliers")),
    )
    # Оба индекса частичные — иначе строки без домена (или без ИНН)
    # конфликтовали бы между собой по NULL.
    op.execute(
        "CREATE UNIQUE INDEX suppliers_domain_uq ON suppliers (lower(domain)) "
        "WHERE domain IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX suppliers_tax_uq ON suppliers (tax_id) WHERE tax_id IS NOT NULL"
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("ordered_at", sa.Date(), nullable=True),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("amount", sa.Numeric(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("promised_date", sa.Date(), nullable=True),
        sa.Column("actual_date", sa.Date(), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name=op.f("ck_orders_rating_range")),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"],
                                name=op.f("fk_orders_supplier_id_suppliers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )

    op.create_table(
        "candidates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("site_claims", sa.Boolean(), nullable=True),
        sa.Column("site_url", sa.Text(), nullable=True),
        sa.Column("site_price", sa.Numeric(), nullable=True),
        sa.Column("ru_number", sa.Text(), nullable=True),
        sa.Column("ru_holder", sa.Text(), nullable=True),
        sa.Column("ru_valid", sa.Boolean(), nullable=True),
        sa.Column("ru_registry", sa.Text(), nullable=True),
        sa.Column("ru_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unrega_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"],
                                name=op.f("fk_candidates_request_id_requests")),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"],
                                name=op.f("fk_candidates_supplier_id_suppliers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidates")),
        sa.UniqueConstraint("request_id", "supplier_id", name="candidates_request_supplier_uq"),
    )
    op.create_index("ix_candidates_request_id", "candidates", ["request_id"])

    op.create_table(
        "quote_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gmail_thread", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reply_text", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("lead_time", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"],
                                name=op.f("fk_quote_requests_request_id_requests")),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"],
                                name=op.f("fk_quote_requests_supplier_id_suppliers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quote_requests")),
    )
    op.create_index("ix_quote_requests_message_id", "quote_requests", ["message_id"])
    op.create_index("ix_quote_requests_gmail_thread", "quote_requests", ["gmail_thread"])
    op.create_index("ix_quote_requests_request_id", "quote_requests", ["request_id"])

    op.create_table(
        "blacklist",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name=op.f("fk_blacklist_order_id_orders")),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"],
                                name=op.f("fk_blacklist_supplier_id_suppliers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blacklist")),
    )
    op.create_index("ix_blacklist_supplier_id", "blacklist", ["supplier_id"])

    op.create_table(
        "criteria",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=True),
        sa.Column("weight", sa.Float(), server_default=sa.text("1.0"), nullable=False),
        sa.Column("times_seen", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_criteria")),
    )
    # Триграммный индекс — только если расширение поднялось выше.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
                CREATE INDEX IF NOT EXISTS ix_criteria_text_trgm
                    ON criteria USING gin (text gin_trgm_ops);
            END IF;
        END $$;
        """
    )

    op.create_table(
        "criteria_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("criterion_id", sa.BigInteger(), nullable=True),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["criterion_id"], ["criteria.id"],
                                name=op.f("fk_criteria_events_criterion_id_criteria")),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"],
                                name=op.f("fk_criteria_events_request_id_requests")),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"],
                                name=op.f("fk_criteria_events_supplier_id_suppliers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_criteria_events")),
    )

    op.create_table(
        "api_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=True),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"],
                                name=op.f("fk_api_calls_request_id_requests")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_calls")),
    )
    op.create_index("ix_api_calls_created_at", "api_calls", ["created_at"])
    op.create_index("ix_api_calls_request_id", "api_calls", ["request_id"])

    op.create_table(
        "registry_cache",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cache_key", sa.String(length=512), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_registry_cache")),
        sa.UniqueConstraint("cache_key", name=op.f("uq_registry_cache_cache_key")),
    )
    op.create_index("ix_registry_cache_checked_at", "registry_cache", ["checked_at"])

    op.create_table(
        "gmail_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("history_id", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gmail_state")),
    )


def downgrade() -> None:
    # Откат обязан работать — это критерий приёмки этапа 1. Порядок обратный
    # созданию, иначе внешние ключи не дадут удалить таблицы.
    op.drop_table("gmail_state")
    op.drop_index("ix_registry_cache_checked_at", table_name="registry_cache")
    op.drop_table("registry_cache")
    op.drop_index("ix_api_calls_request_id", table_name="api_calls")
    op.drop_index("ix_api_calls_created_at", table_name="api_calls")
    op.drop_table("api_calls")
    op.drop_table("criteria_events")
    op.execute("DROP INDEX IF EXISTS ix_criteria_text_trgm")
    op.drop_table("criteria")
    op.drop_index("ix_blacklist_supplier_id", table_name="blacklist")
    op.drop_table("blacklist")
    op.drop_index("ix_quote_requests_request_id", table_name="quote_requests")
    op.drop_index("ix_quote_requests_gmail_thread", table_name="quote_requests")
    op.drop_index("ix_quote_requests_message_id", table_name="quote_requests")
    op.drop_table("quote_requests")
    op.drop_index("ix_candidates_request_id", table_name="candidates")
    op.drop_table("candidates")
    op.drop_table("orders")
    op.execute("DROP INDEX IF EXISTS suppliers_tax_uq")
    op.execute("DROP INDEX IF EXISTS suppliers_domain_uq")
    op.drop_table("suppliers")
    op.drop_table("requests")
    # Расширение не удаляем: его могли поставить не мы и им могут пользоваться
    # другие схемы в той же базе.
