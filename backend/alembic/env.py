from sqlalchemy import create_engine, pool

from alembic import context
from cookops.config import Settings
from cookops.persistence.models import Base

configuration = context.config
target_metadata = Base.metadata


def database_url() -> str:
    configured_url = configuration.get_main_option("sqlalchemy.url")
    return configured_url or str(Settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
