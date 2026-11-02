"""Versioned Pondi / ePoslovanje document status tables.

Source: ePoslovanje API v2 DocumentStatusEnum (1–9). Meaning changes require a
new mapping_version.
"""

from __future__ import annotations

from app.gateway.adapters.super.mapping import InboundMapping, MappedAxis, OutboundMapping

MAPPING_VERSION = 'pondi-v1'

STATUS_TEXT = {
    1: 'Na čekanju',
    2: 'Na slanju',
    3: 'Poslan',
    4: 'Isporučen',
    5: 'Odobren',
    6: 'Odbijen',
    7: 'Plaćen u potpunosti',
    8: 'Parcijalno plaćen',
    9: 'Neuspješna isporuka',
}

# Pondi process status → independent canonical axes.
_OUTBOUND_VALUES = {
    1: ('QUEUED', 'PENDING', 'PENDING', 'UNPAID'),
    2: ('SUBMITTING', 'PENDING', 'PENDING', 'UNPAID'),
    3: ('SUBMITTED', 'PENDING', 'PENDING', 'UNPAID'),
    4: ('DELIVERED', 'PENDING', 'PENDING', 'UNPAID'),
    5: ('DELIVERED', 'PENDING', 'ACCEPTED', 'UNPAID'),
    6: ('DELIVERED', 'PENDING', 'REJECTED', 'UNPAID'),
    7: ('DELIVERED', 'PENDING', 'ACCEPTED', 'PAID'),
    8: ('DELIVERED', 'PENDING', 'ACCEPTED', 'PARTIALLY_PAID'),
    9: ('FAILED', 'PENDING', 'PENDING', 'UNPAID'),
}

_INBOUND_VALUES = {
    1: ('RECEIVED', 'PENDING', 'NONE'),
    2: ('RECEIVED', 'PENDING', 'NONE'),
    3: ('RECEIVED', 'PENDING', 'NONE'),
    4: ('AVAILABLE', 'PENDING', 'NONE'),
    5: ('AVAILABLE', 'PENDING', 'NONE'),
    6: ('AVAILABLE', 'PENDING', 'NONE'),
    7: ('AVAILABLE', 'PENDING', 'NONE'),
    8: ('AVAILABLE', 'PENDING', 'NONE'),
    9: ('FAILED', 'PENDING', 'NONE'),
}


def parse_pondi_code(value) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _axis(value: str, code: int, text: str) -> MappedAxis:
    return MappedAxis(
        value=value,
        mapping_version=MAPPING_VERSION,
        provider_code=str(code),
        provider_text=text,
    )


def _unknown_axis(code) -> MappedAxis:
    return MappedAxis(
        value='UNKNOWN',
        mapping_version=MAPPING_VERSION,
        provider_code=str(code),
        provider_text='Unknown Pondi status',
    )


def map_outbound_status(code) -> OutboundMapping:
    parsed = parse_pondi_code(code)
    if parsed is None or parsed not in _OUTBOUND_VALUES:
        unknown = _unknown_axis(parsed if parsed is not None else code)
        return OutboundMapping(
            exchange=unknown,
            fiscalization=unknown,
            recipient=unknown,
            payment=unknown,
            known=False,
        )
    exchange, fiscalization, recipient, payment = _OUTBOUND_VALUES[parsed]
    text = STATUS_TEXT[parsed]
    return OutboundMapping(
        exchange=_axis(exchange, parsed, text),
        fiscalization=_axis(fiscalization, parsed, text),
        recipient=_axis(recipient, parsed, text),
        payment=_axis(payment, parsed, text),
        known=True,
    )


def map_inbound_status(code) -> InboundMapping:
    parsed = parse_pondi_code(code)
    if parsed is None or parsed not in _INBOUND_VALUES:
        unknown = _unknown_axis(parsed if parsed is not None else code)
        return InboundMapping(
            intake=unknown,
            intake_fiscalization=unknown,
            e_reporting=unknown,
            known=False,
        )
    intake, fiscalization, reporting = _INBOUND_VALUES[parsed]
    text = STATUS_TEXT[parsed]
    return InboundMapping(
        intake=_axis(intake, parsed, text),
        intake_fiscalization=_axis(fiscalization, parsed, text),
        e_reporting=_axis(reporting, parsed, text),
        known=True,
    )


def ubl_document_type(document_type: str) -> str | None:
    mapping = {'INVOICE': 'Invoice', 'CREDIT_NOTE': 'CreditNote'}
    return mapping.get(document_type)


def list_item_id(item: dict) -> str:
    value = item.get('id')
    if value is None:
        return ''
    return str(value)


def known_outbound_codes() -> tuple[int, ...]:
    return tuple(sorted(_OUTBOUND_VALUES))


def known_inbound_codes() -> tuple[int, ...]:
    return tuple(sorted(_INBOUND_VALUES))
