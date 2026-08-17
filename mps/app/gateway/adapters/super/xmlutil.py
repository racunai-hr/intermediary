from __future__ import annotations

import base64
import re

from lxml import etree

from app.gateway.errors import invalid_ubl

_DOCTYPE = re.compile(br'<!DOCTYPE', re.IGNORECASE)
_ENTITY = re.compile(br'<!ENTITY', re.IGNORECASE)


def decode_strict_b64(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise invalid_ubl('Provider Base64 payload is not strict.')
    padded = value + '=' * (-len(value) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except Exception as exc:
        raise invalid_ubl('Provider Base64 payload is not strict.') from exc


def encode_ubl_b64(ubl: str) -> str:
    return base64.b64encode(ubl.encode('utf-8')).decode('ascii')


def parse_ubl_xml(raw: bytes | str) -> str:
    data = raw.encode('utf-8') if isinstance(raw, str) else raw
    if _DOCTYPE.search(data) or _ENTITY.search(data):
        raise invalid_ubl('XML with DTD or entities is not allowed.')
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
    )
    try:
        etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise invalid_ubl('UBL is not well-formed XML.') from exc
    return data.decode('utf-8')
