"""Pondi / ePoslovanje credential resolution.

Env JSON is a transitional backend. API keys never leave this module as
API/error/log fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from app.gateway.errors import provider_not_configured
from app.gateway.settings import get_gateway_settings


@dataclass(frozen=True)
class PondiCredential:
    credential_ref: str
    base_url: str
    api_key: str
    vat_id: str
    software_id: str

    @property
    def account_key(self) -> str:
        return self.vat_id

    def __repr__(self) -> str:
        return (
            f'PondiCredential(credential_ref={self.credential_ref!r}, '
            f'vat_id={self.vat_id!r})'
        )


class CredentialResolver(Protocol):
    def resolve(self, credential_ref: str | None) -> PondiCredential: ...


def _require_https(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise provider_not_configured('Provider base_url must be HTTPS without credentials.')
    return base_url.rstrip('/')


class EnvJsonCredentialResolver:
    """Temporary resolver: GATEWAY_PONDI_CREDENTIALS_JSON."""

    def resolve(self, credential_ref: str | None) -> PondiCredential:
        if not credential_ref or not str(credential_ref).strip():
            raise provider_not_configured('credential_ref is missing.')
        settings = get_gateway_settings()
        try:
            payload = json.loads(settings.pondi_credentials_json or '{}')
        except json.JSONDecodeError as exc:
            raise provider_not_configured('Credential store is not valid JSON.') from exc
        if not isinstance(payload, dict):
            raise provider_not_configured('Credential store must be a JSON object.')
        raw = payload.get(credential_ref)
        if not isinstance(raw, dict):
            raise provider_not_configured('credential_ref is unknown.')
        api_key = raw.get('api_key')
        vat_id = raw.get('vat_id')
        base_url = raw.get('base_url')
        software_id = raw.get('software_id') or 'RACUNAI'
        if not api_key or not vat_id or not base_url:
            raise provider_not_configured('credential_ref is incomplete.')
        if not isinstance(api_key, str) or not isinstance(vat_id, str) or not isinstance(base_url, str):
            raise provider_not_configured('credential_ref is incomplete.')
        if not isinstance(software_id, str) or not software_id.strip():
            raise provider_not_configured('credential_ref is incomplete.')
        return PondiCredential(
            credential_ref=credential_ref,
            base_url=_require_https(base_url),
            api_key=api_key,
            vat_id=vat_id,
            software_id=software_id.strip(),
        )


def default_resolver() -> CredentialResolver:
    return EnvJsonCredentialResolver()
