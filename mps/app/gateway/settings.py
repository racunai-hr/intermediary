from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='GATEWAY_', extra='ignore')

    database_url: str
    jwt_secret: str
    jwt_iss: str = 'racunai-api'
    jwt_aud: str = 'racunai-intermediary'
    jwt_algorithm: str = 'HS256'

    @field_validator('database_url')
    @classmethod
    def reject_sqlite(cls, value: str) -> str:
        if not value:
            raise ValueError('GATEWAY_DATABASE_URL is required')
        lowered = value.lower()
        if lowered.startswith('sqlite:') or 'sqlite' in lowered.split(':', 1)[0]:
            raise ValueError('SQLite is not allowed; use postgresql+psycopg://…@postgis:5432/…')
        if not lowered.startswith('postgresql'):
            raise ValueError('GATEWAY_DATABASE_URL must be a PostgreSQL URL')
        return value


@lru_cache
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()
