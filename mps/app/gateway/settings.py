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
    # Transitional secret backend. Replace with a real secret store; do not treat env JSON as permanent.
    super_credentials_json: str = '{}'
    super_lookup_credential_ref: str = ''
    super_connect_timeout: float = 5.0
    super_read_timeout: float = 30.0
    super_write_timeout: float = 30.0
    super_max_response_bytes: int = 5_000_000
    super_token_skew_seconds: int = 60
    super_lease_seconds: int = 60
    super_poll_overlap_days: int = 2
    super_read_429_max_retries: int = 5

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
