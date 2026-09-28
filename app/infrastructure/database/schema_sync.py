"""Schema sync — models are the source of truth. Called from DatabaseSeeder."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.infrastructure.database.session import Base

# Old → new table names (one-time, preserves data).
_TABLE_RENAMES: list[tuple[str, str]] = [
    ("roles", "platform_roles"),
    ("organization_products", "organization_product_entitlements"),
    ("user_products", "user_product_assignments"),
    ("menu_sections", "navigation_sections"),
    ("menu_items", "navigation_items"),
]

# (table, old_column, new_column)
_COLUMN_RENAMES: list[tuple[str, str, str]] = [
    ("navigation_items", "required_role", "required_role_code"),
]


def sync_schema(engine: Engine) -> None:
    """Rename legacy tables/columns, create missing tables, add missing columns."""
    import app.infrastructure.database.models  # noqa: F401

    _rename_legacy_tables(engine)
    Base.metadata.create_all(bind=engine)
    _rename_legacy_columns(engine)
    _add_missing_columns(engine)


def _rename_legacy_tables(engine: Engine) -> None:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for old_name, new_name in _TABLE_RENAMES:
            if old_name in existing and new_name not in existing:
                if dialect == "sqlite":
                    conn.execute(text(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"'))
                else:
                    conn.execute(text(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"'))
                existing.discard(old_name)
                existing.add(new_name)


def _rename_legacy_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for table_name, old_col, new_col in _COLUMN_RENAMES:
            if table_name not in inspector.get_table_names():
                continue
            cols = {c["name"] for c in inspector.get_columns(table_name)}
            if old_col in cols and new_col not in cols:
                if dialect == "sqlite":
                    # SQLite 3.25+ supports RENAME COLUMN
                    conn.execute(
                        text(
                            f'ALTER TABLE "{table_name}" RENAME COLUMN "{old_col}" TO "{new_col}"'
                        )
                    )
                else:
                    conn.execute(
                        text(
                            f'ALTER TABLE "{table_name}" RENAME COLUMN "{old_col}" TO "{new_col}"'
                        )
                    )


def _add_missing_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if table_name not in inspector.get_table_names():
                continue
            existing = {col["name"] for col in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                # Prefer nullable ADD for additive safety; NOT NULL columns with
                # server defaults / Python defaults still need a DEFAULT for existing rows.
                default_sql = ""
                if not column.nullable:
                    if column.name == "product_code":
                        default_sql = " DEFAULT ''"
                    elif hasattr(column.type, "python_type") and column.type.python_type is bool:
                        default_sql = " DEFAULT FALSE" if engine.dialect.name != "sqlite" else " DEFAULT 0"
                    elif column.name in ("is_built_in", "is_coming_soon", "is_active"):
                        default_sql = " DEFAULT FALSE" if engine.dialect.name != "sqlite" else " DEFAULT 0"
                    else:
                        # Skip non-nullable without safe default — create_all already handled new tables
                        continue
                null_sql = "" if not column.nullable else ""
                try:
                    conn.execute(
                        text(
                            f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" '
                            f"{col_type}{default_sql}{null_sql}"
                        )
                    )
                except Exception:
                    # Column may already exist after rename race; ignore
                    pass
