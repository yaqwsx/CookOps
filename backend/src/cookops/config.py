from enum import StrEnum
from typing import Self

from pydantic import model_validator
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

    @model_validator(mode="after")
    def reject_dummy_auth_in_production(self) -> Self:
        if (
            self.environment is Environment.PRODUCTION
            and self.human_auth_provider is HumanAuthProvider.DUMMY
        ):
            raise ValueError("dummy authentication cannot be enabled in production")
        return self
