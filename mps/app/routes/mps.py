from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response

from app.certificate import CertificateError, load_p12
from app.config import get_settings
from app.metadata import build_signed_service_metadata

logger = logging.getLogger(__name__)

router = APIRouter()


def _load_material():
    settings = get_settings()
    if not settings.cert_p12_password:
        raise HTTPException(status_code=500, detail='MPS_CERT_P12_PASSWORD nije postavljen')
    try:
        return settings, load_p12(settings.cert_p12_path, settings.cert_p12_password)
    except CertificateError as exc:
        logger.exception('Certifikat')
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get('/EracunMPS/{participant}/services/{document_type_id}')
def get_service_metadata(participant: str, document_type_id: str):
    settings, material = _load_material()
    xml_bytes = build_signed_service_metadata(
        participant,
        document_type_id,
        settings,
        material,
    )
    if xml_bytes is None:
        raise HTTPException(status_code=404, detail='Metapodaci nisu pronađeni')
    return Response(content=xml_bytes, media_type='text/xml; charset=utf-8')
