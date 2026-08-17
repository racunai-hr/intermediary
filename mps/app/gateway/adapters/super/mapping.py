"""Versioned Super status tables.

Source: Super Web API 5.0 (November 2025) plus Super portal changelog
2026-04-19 (inbound 30 Approved, 50 Liquidated). Meaning changes require a
new mapping_version.
"""

from __future__ import annotations

from dataclasses import dataclass

MAPPING_VERSION = 'super-v1'

# eSendingInvoiceStatusEnum
OUTBOUND_STATUS_TEXT = {
    10: 'Draft',
    40: 'Sent',
    50: 'Sending error',
    55: 'Delivery failure',
    60: 'Delivered',
    90: 'Rejected',
    100: 'Partially paid',
    110: 'Paid',
}

# eInvoiceStatusEnum
INBOUND_STATUS_TEXT = {
    10: 'Received',
    30: 'Approved',
    40: 'Rejected',
    50: 'Liquidated',
}

# eFiscalizationPaymentMethodTypeEnum
PAYMENT_METHOD_TEXT = {
    1: 'Transaction account',
    2: 'Clearing settlement',
    11: 'Other',
}

# eFiscalizationInvoiceRejectReasonTypeEnum
REJECT_REASON_TEXT = {
    1: 'Mismatch that does not affect tax calculation',
    2: 'Mismatch that affects tax calculation',
    11: 'Other',
}

UBL_DOCUMENT_TYPE = {
    'INVOICE': 1,
    'CREDIT_NOTE': 2,
}

CANONICAL_PAYMENT_METHOD = {
    'BANK_TRANSFER': 1,
    'TRANSACTION_ACCOUNT': 1,
    'CLEARING': 2,
    'OTHER': 11,
}

CANONICAL_REJECT_REASON = {
    'TAX_NEUTRAL_MISMATCH': 1,
    'TAX_AFFECTING_MISMATCH': 2,
    'OTHER': 11,
    'REJECTED_BY_RECIPIENT': 11,
}


@dataclass(frozen=True)
class MappedAxis:
    value: str
    mapping_version: str
    provider_code: str
    provider_text: str


@dataclass(frozen=True)
class OutboundMapping:
    exchange: MappedAxis
    fiscalization: MappedAxis
    recipient: MappedAxis
    payment: MappedAxis
    known: bool


@dataclass(frozen=True)
class InboundMapping:
    intake: MappedAxis
    intake_fiscalization: MappedAxis
    e_reporting: MappedAxis
    known: bool


def _axis(value: str, code: int, text: str) -> MappedAxis:
    return MappedAxis(
        value=value,
        mapping_version=MAPPING_VERSION,
        provider_code=str(code),
        provider_text=text,
    )


def _unknown_axis(code: int) -> MappedAxis:
    return MappedAxis(
        value='UNKNOWN',
        mapping_version=MAPPING_VERSION,
        provider_code=str(code),
        provider_text='Unknown Super status',
    )


# Super outbound code → independent canonical axes. One Super code may move several axes.
_OUTBOUND_VALUES = {
    10: ('QUEUED', 'PENDING', 'PENDING', 'UNPAID'),
    40: ('SUBMITTED', 'PENDING', 'PENDING', 'UNPAID'),
    50: ('FAILED', 'PENDING', 'PENDING', 'UNPAID'),
    55: ('FAILED', 'PENDING', 'PENDING', 'UNPAID'),
    60: ('DELIVERED', 'PENDING', 'PENDING', 'UNPAID'),
    90: ('DELIVERED', 'PENDING', 'REJECTED', 'UNPAID'),
    100: ('DELIVERED', 'PENDING', 'PENDING', 'PARTIALLY_PAID'),
    110: ('DELIVERED', 'PENDING', 'PENDING', 'PAID'),
}

# Super inbound InvoiceStatus is not legal e-reporting and is not racunAI workflow.
# 30/40/50 stay on intake; e_reporting is not inferred from SetInvoiceStatus-style codes.
_INBOUND_VALUES = {
    10: ('AVAILABLE', 'PENDING', 'NONE'),
    30: ('AVAILABLE', 'PENDING', 'NONE'),
    40: ('AVAILABLE', 'PENDING', 'NONE'),
    50: ('AVAILABLE', 'PENDING', 'NONE'),
}


def parse_super_code(value) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def map_outbound_status(code) -> OutboundMapping:
    parsed = parse_super_code(code)
    if parsed is None or parsed not in _OUTBOUND_VALUES:
        raw = parsed if parsed is not None else 0
        unknown = _unknown_axis(raw if parsed is not None else 0)
        if parsed is None:
            unknown = MappedAxis(
                value='UNKNOWN',
                mapping_version=MAPPING_VERSION,
                provider_code=str(code),
                provider_text='Unknown Super status',
            )
        return OutboundMapping(
            exchange=unknown,
            fiscalization=unknown,
            recipient=unknown,
            payment=unknown,
            known=False,
        )
    exchange, fiscalization, recipient, payment = _OUTBOUND_VALUES[parsed]
    text = OUTBOUND_STATUS_TEXT[parsed]
    return OutboundMapping(
        exchange=_axis(exchange, parsed, text),
        fiscalization=_axis(fiscalization, parsed, text),
        recipient=_axis(recipient, parsed, text),
        payment=_axis(payment, parsed, text),
        known=True,
    )


def map_inbound_status(code) -> InboundMapping:
    parsed = parse_super_code(code)
    if parsed is None or parsed not in _INBOUND_VALUES:
        unknown = MappedAxis(
            value='UNKNOWN',
            mapping_version=MAPPING_VERSION,
            provider_code=str(code) if parsed is None else str(parsed),
            provider_text='Unknown Super status',
        )
        return InboundMapping(
            intake=unknown,
            intake_fiscalization=unknown,
            e_reporting=unknown,
            known=False,
        )
    intake, fiscalization, reporting = _INBOUND_VALUES[parsed]
    text = INBOUND_STATUS_TEXT[parsed]
    return InboundMapping(
        intake=_axis(intake, parsed, text),
        intake_fiscalization=_axis(fiscalization, parsed, text),
        e_reporting=_axis(reporting, parsed, text),
        known=True,
    )


def ubl_document_type(document_type: str) -> int | None:
    return UBL_DOCUMENT_TYPE.get(document_type)


def payment_method_code(method: str) -> int:
    return CANONICAL_PAYMENT_METHOD.get(str(method).upper(), 11)


def reject_reason_code(reason_code: str) -> int:
    return CANONICAL_REJECT_REASON.get(str(reason_code).upper(), 11)


def known_outbound_codes() -> tuple[int, ...]:
    return tuple(sorted(OUTBOUND_STATUS_TEXT))


def known_inbound_codes() -> tuple[int, ...]:
    return tuple(sorted(INBOUND_STATUS_TEXT))
