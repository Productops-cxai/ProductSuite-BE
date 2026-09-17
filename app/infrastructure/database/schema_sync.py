"""Schema sync — models are the source of truth. Called from DatabaseSeeder."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.infrastructure.database.session import Base


def sync_schema(engine: Engine) -> None:
    """Create missing tables and add missing nullable columns (additive)."""
    import app.infrastructure.database.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _add_missing_columns(engine)


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
                if column.nullable:
                    conn.execute(
                        text(
                            f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}'
                        )
                    )
