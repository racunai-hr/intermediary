from __future__ import annotations

import os
from pathlib import Path

import pytest
from lxml import etree

from app.certificate import load_p12
from app.config import DOCUMENT_ID, Settings
from app.metadata import build_signed_service_metadata

CERT_PATH = Path('/opt/stacks/racunai.hr/.certificates/36619131370.F2.2.p12')
CERT_PASSWORD = os.environ.get('MPS_CERT_P12_PASSWORD', 'Orangepi.123')
PARTICIPANT = 'iso6523-actorid-upis::9934:36619131370'


@pytest.mark.parametrize(
    'participant_oib',
    ['36619131370', '36098855113'],
)
@pytest.mark.skipif(not CERT_PATH.is_file(), reason='Demo cert nije dostupan')
def test_build_signed_service_metadata(participant_oib):
    settings = Settings(
        ap_oib='36619131370',
        as4_endpoint='https://as4-test.racunai.hr/EracunAS4/services/msh',
        cert_p12_path=str(CERT_PATH),
        cert_p12_password=CERT_PASSWORD,
    )
    material = load_p12(settings.cert_p12_path, settings.cert_p12_password)
    participant = f'iso6523-actorid-upis::9934:{participant_oib}'
    xml_bytes = build_signed_service_metadata(
        participant,
        DOCUMENT_ID,
        settings,
        material,
    )
    assert xml_bytes is not None
    root = etree.fromstring(xml_bytes)
    assert root.tag.endswith('SignedServiceMetadata')
    assert root.findtext('.//{*}EndpointURI') == settings.as4_endpoint
    assert root.find('.//{*}Signature') is not None
    transforms = root.findall(
        './/{http://www.w3.org/2000/09/xmldsig#}Reference'
        '/{http://www.w3.org/2000/09/xmldsig#}Transforms'
        '/{http://www.w3.org/2000/09/xmldsig#}Transform'
    )
    assert len(transforms) == 1
    assert transforms[0].get('Algorithm') == (
        'http://www.w3.org/2000/09/xmldsig#enveloped-signature'
    )
    assert root.findtext('.//{*}ParticipantOIB') == participant_oib
    assert root.findtext('.//{*}AccessPointOIB') == settings.ap_oib


@pytest.mark.skipif(not CERT_PATH.is_file(), reason='Demo cert nije dostupan')
def test_build_signed_service_metadata_accepts_truncated_document_path():
    settings = Settings(
        ap_oib='36619131370',
        as4_endpoint='https://as4-test.racunai.hr/EracunAS4/services/msh',
        cert_p12_path=str(CERT_PATH),
        cert_p12_password=CERT_PASSWORD,
    )
    material = load_p12(settings.cert_p12_path, settings.cert_p12_password)
    truncated_document_id = DOCUMENT_ID.split('##', 1)[0]
    xml_bytes = build_signed_service_metadata(
        'iso6523-actorid-upis::9934:41851854549',
        truncated_document_id,
        settings,
        material,
    )
    assert xml_bytes is not None
