from __future__ import annotations

import logging
import os
import ssl
import tempfile
from dataclasses import dataclass

import httpx
from lxml import etree

from app.certificate import CertificateMaterial, load_p12
from app.config import Settings

logger = logging.getLogger(__name__)

LOCATOR_NS = 'http://busdox.org/serviceMetadata/locator/1.0/'
IDS_NS = 'http://busdox.org/transport/identifiers/1.0/'
SOAP_ENV_NS = 'http://schemas.xmlsoap.org/soap/envelope/'
SOAP_ACTION_BASE = (
    'http://busdox.org/serviceMetadata/ManageBusinessIdentifierService/1.0/'
)


class AmsError(Exception):
    pass


@dataclass(frozen=True)
class AmsParticipant:
    scheme: str
    identifier: str

    @property
    def full(self) -> str:
        return f'{self.scheme}::{self.identifier}'


def participant_for_oib(oib: str) -> AmsParticipant:
    return AmsParticipant(scheme='iso6523-actorid-upis', identifier=f'9934:{oib}')


class AmsClient:
    def __init__(self, settings: Settings, material: CertificateMaterial):
        self.settings = settings
        self.material = material
        self._cert_path, self._key_path = self._write_client_cert_files()

    def _soap_envelope(self, body: etree._Element) -> bytes:
        envelope = etree.Element(
            f'{{{SOAP_ENV_NS}}}Envelope',
            nsmap={'S': SOAP_ENV_NS},
        )
        soap_body = etree.SubElement(envelope, f'{{{SOAP_ENV_NS}}}Body')
        soap_body.append(body)
        return etree.tostring(
            envelope,
            xml_declaration=True,
            encoding='UTF-8',
        )

    def _build_create_body(self, participant: AmsParticipant) -> etree._Element:
        body = etree.Element(
            f'{{{LOCATOR_NS}}}CreateParticipantIdentifier',
            nsmap={
                None: LOCATOR_NS,
                'ids': IDS_NS,
            },
        )
        publisher = etree.SubElement(body, f'{{{LOCATOR_NS}}}ServiceMetadataPublisherID')
        publisher.text = self.settings.publisher_id
        participant_el = etree.SubElement(
            body,
            f'{{{IDS_NS}}}ParticipantIdentifier',
            scheme=participant.scheme,
        )
        participant_el.text = participant.identifier
        return body

    def _build_delete_body(self, participant: AmsParticipant) -> etree._Element:
        body = etree.Element(
            f'{{{LOCATOR_NS}}}DeleteParticipantIdentifier',
            nsmap={
                None: LOCATOR_NS,
                'ids': IDS_NS,
            },
        )
        publisher = etree.SubElement(body, f'{{{LOCATOR_NS}}}ServiceMetadataPublisherID')
        publisher.text = self.settings.publisher_id
        participant_el = etree.SubElement(
            body,
            f'{{{IDS_NS}}}ParticipantIdentifier',
            scheme=participant.scheme,
        )
        participant_el.text = participant.identifier
        return body

    def _build_list_body(self) -> etree._Element:
        body = etree.Element(
            f'{{{LOCATOR_NS}}}PageRequest',
            nsmap={None: LOCATOR_NS},
        )
        publisher = etree.SubElement(
            body,
            f'{{{LOCATOR_NS}}}ServiceMetadataPublisherID',
        )
        publisher.text = self.settings.publisher_id
        return body

    def _write_client_cert_files(self) -> tuple[str, str]:
        cert_fd, cert_path = tempfile.mkstemp(suffix='.pem')
        key_fd, key_path = tempfile.mkstemp(suffix='.key')
        try:
            os.write(cert_fd, self.material.certificate_pem)
            for chain_pem in self.material.chain_pem:
                os.write(cert_fd, chain_pem)
            os.write(key_fd, self.material.private_key_pem)
        finally:
            os.close(cert_fd)
            os.close(key_fd)
        return cert_path, key_path

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if not self.settings.ams_verify_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        context.load_cert_chain(certfile=self._cert_path, keyfile=self._key_path)
        return context

    def _call(self, action_suffix: str, body: etree._Element) -> etree._Element:
        payload = self._soap_envelope(body)
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': f'{SOAP_ACTION_BASE}:{action_suffix}',
        }
        context = self._ssl_context()
        try:
            with httpx.Client(verify=context, timeout=60.0) as client:
                response = client.post(
                    self.settings.ams_proxy_url,
                    content=payload,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise AmsError(f'AMS HTTP greška: {exc}') from exc

        if response.status_code >= 400:
            raise AmsError(
                f'AMS HTTP {response.status_code}: {response.text[:500]}'
            )

        try:
            root = etree.fromstring(response.content)
        except etree.XMLSyntaxError as exc:
            raise AmsError(f'AMS odgovor nije valjan XML: {exc}') from exc

        fault = root.find(f'.//{{{SOAP_ENV_NS}}}Fault')
        if fault is not None:
            fault_string = fault.findtext('faultstring') or etree.tostring(
                fault,
                encoding='unicode',
            )
            raise AmsError(f'AMS SOAP Fault: {fault_string[:500]}')

        body_el = root.find(f'.//{{{SOAP_ENV_NS}}}Body')
        if body_el is None or len(body_el) == 0:
            raise AmsError('AMS odgovor ne sadrži SOAP Body')
        return body_el[0]

    def create(self, participant: AmsParticipant) -> etree._Element:
        return self._call('createIn', self._build_create_body(participant))

    def delete(self, participant: AmsParticipant) -> etree._Element:
        return self._call('deleteIn', self._build_delete_body(participant))

    def list_participants(self) -> etree._Element:
        return self._call('listIn', self._build_list_body())


def create_ams_client(settings: Settings) -> AmsClient:
    if not settings.cert_p12_password:
        raise AmsError('MPS_CERT_P12_PASSWORD nije postavljen')
    material = load_p12(settings.cert_p12_path, settings.cert_p12_password)
    return AmsClient(settings, material)


def parse_participant_page(response_el: etree._Element) -> list[AmsParticipant]:
    participants: list[AmsParticipant] = []
    for el in response_el.iter():
        if etree.QName(el).localname != 'ParticipantIdentifier':
            continue
        scheme = el.get('scheme') or ''
        value = (el.text or '').strip()
        if scheme and value:
            participants.append(AmsParticipant(scheme=scheme, identifier=value))
    return participants
