from __future__ import annotations

from datetime import datetime, timezone

from lxml import etree

from app.certificate import CertificateMaterial
from app.config import (
    DOCUMENT_ID,
    DOCUMENT_SCHEME,
    PARTICIPANT_SCHEME,
    PROCESS_ID,
    PROCESS_SCHEME,
    TRANSPORT_PROFILE,
    Settings,
)
from app.signing import sign_signed_service_metadata

SMP_NS = 'http://docs.oasis-open.org/bdxr/ns/SMP/2016/05'
EXT_NS = 'http://porezna-uprava.hr/mps/extension'
DOCUMENT_ID_BASE = DOCUMENT_ID.split('##', 1)[0]


def _matches_document_type_id(document_type_id: str) -> bool:
    if document_type_id == DOCUMENT_ID:
        return True
    if document_type_id == DOCUMENT_ID_BASE:
        return True
    if document_type_id.startswith(f'{DOCUMENT_ID_BASE}##'):
        return True
    return False


def _parse_participant(participant: str) -> tuple[str, str] | None:
    if '::' not in participant:
        return None
    scheme, value = participant.split('::', 1)
    if not scheme or not value:
        return None
    return scheme, value


def _parse_document_id(document_type_id: str) -> tuple[str, str] | None:
    if '::' not in document_type_id:
        return None
    scheme, value = document_type_id.split('::', 1)
    if not scheme or not value:
        return None
    return scheme, value


def build_signed_service_metadata(
    participant: str,
    document_type_id: str,
    settings: Settings,
    material: CertificateMaterial,
) -> bytes | None:
    parsed_participant = _parse_participant(participant)
    parsed_document = _parse_document_id(document_type_id)
    if not parsed_participant or not parsed_document:
        return None

    participant_scheme, participant_value = parsed_participant
    document_scheme, document_value = parsed_document

    if participant_scheme != PARTICIPANT_SCHEME:
        return None
    if not _matches_document_type_id(document_type_id):
        return None

    participant_oib = participant_value.split(':', 1)[-1]
    if not participant_oib.isdigit() or len(participant_oib) != 11:
        return None

    now = datetime.now(timezone.utc)
    activation = now.strftime('%Y-%m-%dT%H:%M:%S') + f'.{now.microsecond // 1000:03d}'

    root = etree.Element(f'{{{SMP_NS}}}SignedServiceMetadata', nsmap={None: SMP_NS})
    service_metadata = etree.SubElement(
        root,
        f'{{{SMP_NS}}}ServiceMetadata',
        nsmap={'ex': EXT_NS},
    )
    service_info = etree.SubElement(service_metadata, f'{{{SMP_NS}}}ServiceInformation')

    participant_el = etree.SubElement(
        service_info,
        f'{{{SMP_NS}}}ParticipantIdentifier',
        scheme=participant_scheme,
    )
    participant_el.text = participant_value

    document_el = etree.SubElement(
        service_info,
        f'{{{SMP_NS}}}DocumentIdentifier',
        scheme=document_scheme,
    )
    document_el.text = document_value

    process_list = etree.SubElement(service_info, f'{{{SMP_NS}}}ProcessList')
    process = etree.SubElement(process_list, f'{{{SMP_NS}}}Process')
    process_id = etree.SubElement(
        process,
        f'{{{SMP_NS}}}ProcessIdentifier',
        scheme=PROCESS_SCHEME,
    )
    process_id.text = PROCESS_ID

    endpoint_list = etree.SubElement(process, f'{{{SMP_NS}}}ServiceEndpointList')
    endpoint = etree.SubElement(
        endpoint_list,
        f'{{{SMP_NS}}}Endpoint',
        transportProfile=TRANSPORT_PROFILE,
    )
    etree.SubElement(endpoint, f'{{{SMP_NS}}}EndpointURI').text = settings.as4_endpoint
    etree.SubElement(endpoint, f'{{{SMP_NS}}}RequireBusinessLevelSignature').text = 'true'
    etree.SubElement(endpoint, f'{{{SMP_NS}}}ServiceActivationDate').text = activation
    etree.SubElement(endpoint, f'{{{SMP_NS}}}Certificate').text = material.certificate_der_b64
    etree.SubElement(endpoint, f'{{{SMP_NS}}}ServiceDescription').text = (
        'Zaprimanje elektroničkih dokumenata'
    )
    etree.SubElement(endpoint, f'{{{SMP_NS}}}TechnicalContactUrl').text = (
        'https://porezna.gov.hr/fiskalizacija'
    )
    etree.SubElement(endpoint, f'{{{SMP_NS}}}TechnicalInformationUrl').text = (
        'https://porezna.gov.hr/fiskalizacija'
    )

    extension = etree.SubElement(service_info, f'{{{SMP_NS}}}Extension')
    etree.SubElement(extension, f'{{{SMP_NS}}}ExtensionID').text = 'eRacunParticipantData'
    hr_mps = etree.SubElement(extension, f'{{{EXT_NS}}}HRMPS')
    etree.SubElement(hr_mps, f'{{{EXT_NS}}}ParticipantOIB').text = participant_oib
    etree.SubElement(hr_mps, f'{{{EXT_NS}}}AccessPointOIB').text = settings.ap_oib

    signed = sign_signed_service_metadata(root, material)
    return etree.tostring(
        signed,
        xml_declaration=True,
        encoding='UTF-8',
        standalone=False,
    )
