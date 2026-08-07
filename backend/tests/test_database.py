import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from alembic import command
from cookops.database import (
    DATABASE_CONNECT_TIMEOUT_SECONDS,
    create_database_runtime,
    load_alembic_head,
)


def test_database_runtime_reports_connection_failure() -> None:
    connection = MagicMock()
    connection.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("unavailable"))
    connection.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection
    engine.dispose = AsyncMock()
    engine_factory = MagicMock(return_value=engine)

    async def exercise_runtime() -> None:
        runtime = create_database_runtime(
            "postgresql+psycopg://cookops:cookops@database/cookops",
            engine_factory=engine_factory,
        )
        try:
            assert await runtime.is_ready() is False
        finally:
            await runtime.close()

    asyncio.run(exercise_runtime())
    engine.dispose.assert_awaited_once_with()
    engine_factory.assert_called_once_with(
        "postgresql+psycopg://cookops:cookops@database/cookops",
        pool_pre_ping=True,
        connect_args={"connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS},
    )


@pytest.mark.skipif("TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set")
def test_database_is_ready_only_at_alembic_head() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.downgrade(configuration, "base")

    async def exercise_runtime() -> None:
        runtime = create_database_runtime(database_url)
        try:
            assert await runtime.is_ready() is False

            command.upgrade(configuration, "head")

            assert await runtime.is_ready() is True
            async with runtime.session_factory() as session:
                result = await session.execute(text("SELECT 1"))
                assert result.scalar_one() == 1
                await session.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES ('unexpected_head')")
                )
                await session.commit()

            assert await runtime.is_ready() is False
            async with runtime.session_factory() as session:
                await session.execute(
                    text("DELETE FROM alembic_version WHERE version_num = 'unexpected_head'")
                )
                await session.commit()
        finally:
            await runtime.close()

    try:
        asyncio.run(exercise_runtime())
        assert load_alembic_head() == "0003_organization_configuration"
    finally:
        command.downgrade(configuration, "base")
