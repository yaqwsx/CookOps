from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_CONNECT_TIMEOUT_SECONDS = 5


def load_alembic_head() -> str:
    configuration = Config(PROJECT_ROOT / "alembic.ini")
    head = ScriptDirectory.from_config(configuration).get_current_head()
    if head is None:
        raise RuntimeError("Alembic migration history has no head revision")
    return head


@dataclass
class DatabaseRuntime:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    expected_revision: str

    async def is_ready(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                current_revisions = await connection.run_sync(
                    lambda sync_connection: MigrationContext.configure(
                        sync_connection
                    ).get_current_heads()
                )
        except SQLAlchemyError:
            return False
        return current_revisions == (self.expected_revision,)

    async def close(self) -> None:
        await self.engine.dispose()


def create_database_runtime(
    database_url: str,
    *,
    engine_factory: Callable[..., AsyncEngine] = create_async_engine,
) -> DatabaseRuntime:
    engine = engine_factory(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS},
    )
    return DatabaseRuntime(
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        expected_revision=load_alembic_head(),
    )
