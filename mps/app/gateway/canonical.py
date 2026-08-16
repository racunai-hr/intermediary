from __future__ import annotations

import base64
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.gateway.errors import invalid_request

_OIB_RE = re.compile(r'^\d{11}$')
_AMOUNT_RE = re.compile(r'^\d+\.\d{2}$')


def require_oib(value: str) -> str:
    if not value or not _OIB_RE.match(value):
        raise invalid_request('taxpayer_oib must be an 11-digit OIB.')
    return value


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def request_hash(*, method: str, path: str, taxpayer_oib: str, body: Any, ubl: str = '') -> str:
    material = '|'.join(
        [
            method.upper(),
            path,
            taxpayer_oib,
            canonical_json(body),
            sha256_text(ubl) if ubl else '',
        ]
    )
    return sha256_text(material)


def validate_amount(amount: str) -> str:
    if not isinstance(amount, str):
        raise invalid_request('amount must be a decimal string.')
    if 'e' in amount.lower() or amount.startswith('-') or amount.startswith('+'):
        raise invalid_request('amount must be a non-negative decimal string without exponent.')
    if not _AMOUNT_RE.match(amount):
        raise invalid_request('amount must have exactly two decimal places.')
    try:
        value = Decimal(amount)
    except InvalidOperation as exc:
        raise invalid_request('amount is not a valid decimal.') from exc
    if value < 0:
        raise invalid_request('amount must not be negative.')
    return amount


def encode_cursor(taxpayer_oib: str, seq: int) -> str:
    raw = f'{taxpayer_oib}:{seq}'.encode('ascii')
    return 'gwy_' + base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_cursor(cursor: str, expected_oib: str) -> int:
    if not cursor.startswith('gwy_'):
        raise invalid_request('cursor is invalid.')
    padded = cursor.removeprefix('gwy_') + '=' * (-len(cursor.removeprefix('gwy_')) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode('ascii')
        oib, seq_text = decoded.split(':', 1)
        seq = int(seq_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise invalid_request('cursor is invalid.') from exc
    if oib != expected_oib:
        raise invalid_request('cursor does not belong to this taxpayer.')
    return seq
