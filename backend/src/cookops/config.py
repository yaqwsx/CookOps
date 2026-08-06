from enum import StrEnum
from typing import Self

from pydantic import PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class HumanAuthProvider(StrEnum):
    DUMMY = "dummy"
    GOOGLE = "google"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COOKOPS_", extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    human_auth_provider: HumanAuthProvider = HumanAuthProvider.DUMMY
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+psycopg://cookops:cookops@localhost:5432/cookops"
    )

    @model_validator(mode="after")
    def validate_deployment_boundaries(self) -> Self:
        if (
            self.environment is Environment.PRODUCTION
            and self.human_auth_provider is HumanAuthProvider.DUMMY
        ):
            raise ValueError("dummy authentication cannot be enabled in production")
        if self.database_url.scheme != "postgresql+psycopg":
            raise ValueError("database URL must use the postgresql+psycopg scheme")
        return self
