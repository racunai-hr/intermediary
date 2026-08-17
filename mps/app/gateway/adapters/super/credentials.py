"""Credential resolution.

Env JSON is a transitional backend, not a secret store. Username and password
never leave this module as API/error/log fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from app.gateway.errors import provider_not_configured
from app.gateway.settings import get_gateway_settings


@dataclass(frozen=True)
class SuperCredential:
    credential_ref: str
    base_url: str
    username: str
    password: str
    company_guid: str

    def __repr__(self) -> str:
        return f'SuperCredential(credential_ref={self.credential_ref!r}, company_guid={self.company_guid!r})'


class CredentialResolver(Protocol):
    def resolve(self, credential_ref: str | None) -> SuperCredential: ...


def _require_https(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise provider_not_configured('Provider base_url must be HTTPS without credentials.')
    return base_url.rstrip('/')


class EnvJsonCredentialResolver:
    """Temporary resolver: GATEWAY_SUPER_CREDENTIALS_JSON. Do not persist as the store."""

    def resolve(self, credential_ref: str | None) -> SuperCredential:
        if not credential_ref or not str(credential_ref).strip():
            raise provider_not_configured('credential_ref is missing.')
        settings = get_gateway_settings()
        try:
            payload = json.loads(settings.super_credentials_json or '{}')
        except json.JSONDecodeError as exc:
            raise provider_not_configured('Credential store is not valid JSON.') from exc
        if not isinstance(payload, dict):
            raise provider_not_configured('Credential store must be a JSON object.')
        raw = payload.get(credential_ref)
        if not isinstance(raw, dict):
            raise provider_not_configured('credential_ref is unknown.')
        username = raw.get('username')
        password = raw.get('password')
        company_guid = raw.get('company_guid')
        base_url = raw.get('base_url')
        if not username or not password or not company_guid or not base_url:
            raise provider_not_configured('credential_ref is incomplete.')
        if not isinstance(username, str) or not isinstance(password, str):
            raise provider_not_configured('credential_ref is incomplete.')
        if not isinstance(company_guid, str) or not isinstance(base_url, str):
            raise provider_not_configured('credential_ref is incomplete.')
        return SuperCredential(
            credential_ref=credential_ref,
            base_url=_require_https(base_url),
            username=username,
            password=password,
            company_guid=company_guid,
        )


def default_resolver() -> CredentialResolver:
    return EnvJsonCredentialResolver()
