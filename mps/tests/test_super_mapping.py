from __future__ import annotations

import pytest

from app.gateway.adapters.super.mapping import (
    MAPPING_VERSION,
    known_inbound_codes,
    known_outbound_codes,
    map_inbound_status,
    map_outbound_status,
    ubl_document_type,
)


@pytest.mark.parametrize('code', known_outbound_codes())
def test_every_known_outbound_status_is_mapped(code):
    mapped = map_outbound_status(code)
    assert mapped.known is True
    assert mapped.exchange.mapping_version == MAPPING_VERSION
    assert mapped.exchange.provider_code == str(code)
    assert mapped.exchange.value != 'UNKNOWN' or code in set()


@pytest.mark.parametrize('code', known_inbound_codes())
def test_every_known_inbound_status_is_mapped(code):
    mapped = map_inbound_status(code)
    assert mapped.known is True
    assert mapped.intake.mapping_version == MAPPING_VERSION
    assert mapped.intake.value == 'AVAILABLE'


def test_unknown_super_status_stays_raw_and_unknown():
    mapped = map_outbound_status(999)
    assert mapped.known is False
    assert mapped.exchange.value == 'UNKNOWN'
    assert mapped.exchange.provider_code == '999'
    inbound = map_inbound_status('xyz')
    assert inbound.known is False
    assert inbound.intake.provider_code == 'xyz'


def test_ubl_types_match_super_spec():
    assert ubl_document_type('INVOICE') == 1
    assert ubl_document_type('CREDIT_NOTE') == 2
    assert ubl_document_type('DEBIT_NOTE') is None
