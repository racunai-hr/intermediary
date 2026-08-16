from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, Header
from jwt import InvalidTokenError

from app.gateway.errors import GatewayError, unauthorized_subject
from app.gateway.settings import get_gateway_settings

ALLOWED_ALGORITHMS = {'HS256'}


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    taxpayers: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def can_act_for(self, taxpayer_oib: str) -> bool:
        return '*' in self.taxpayers or taxpayer_oib in self.taxpayers


def _split_scopes(value) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, list):
        return frozenset(str(item) for item in value)
    return frozenset(part for part in str(value).split() if part)


def _split_taxpayers(value) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, list):
        return frozenset(str(item) for item in value)
    return frozenset(part for part in str(value).split() if part)


def decode_principal(authorization: str | None) -> Principal:
    settings = get_gateway_settings()
    if not authorization or not authorization.startswith('Bearer '):
        raise GatewayError('INVALID_REQUEST', 'Authorization Bearer token is required.', 401)
    token = authorization.removeprefix('Bearer ').strip()
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise GatewayError('INVALID_REQUEST', 'JWT is malformed.', 401) from exc
    alg = header.get('alg')
    if alg not in ALLOWED_ALGORITHMS or alg != settings.jwt_algorithm:
        raise GatewayError('INVALID_REQUEST', 'JWT algorithm is not allowed.', 401)
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_aud,
            issuer=settings.jwt_iss,
            options={'require': ['iss', 'aud', 'exp', 'jti']},
        )
    except InvalidTokenError as exc:
        raise GatewayError('INVALID_REQUEST', f'JWT is invalid: {exc}.', 401) from exc
    subject = str(claims.get('sub') or claims.get('iss'))
    return Principal(
        subject=subject,
        scopes=_split_scopes(claims.get('scope') or claims.get('scopes')),
        taxpayers=_split_taxpayers(claims.get('taxpayers')),
    )


def get_principal(authorization: str | None = Header(default=None)) -> Principal:
    return decode_principal(authorization)


def require_scope(scope: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.has_scope(scope):
            raise GatewayError('INVALID_REQUEST', f'Scope {scope} is required.', 403)
        return principal

    return dependency


def require_taxpayer(principal: Principal, taxpayer_oib: str) -> None:
    if not principal.can_act_for(taxpayer_oib):
        raise unauthorized_subject()
