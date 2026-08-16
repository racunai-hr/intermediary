from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from lxml import etree
from pydantic import BaseModel, Field

from app.ams.client import AmsError, create_ams_client, parse_participant_page, participant_for_oib
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/admin/ams', tags=['ams-admin'])


class AmsOibRequest(BaseModel):
    oib: str = Field(min_length=11, max_length=11)


@router.post('/create')
def ams_create(request: AmsOibRequest):
    settings = get_settings()
    try:
        client = create_ams_client(settings)
        response = client.create(participant_for_oib(request.oib))
    except AmsError as exc:
        logger.exception('AMS create')
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {'status': 'ok', 'action': 'create', 'oib': request.oib, 'response': _element_name(response)}


@router.post('/delete')
def ams_delete(request: AmsOibRequest):
    settings = get_settings()
    try:
        client = create_ams_client(settings)
        response = client.delete(participant_for_oib(request.oib))
    except AmsError as exc:
        logger.exception('AMS delete')
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {'status': 'ok', 'action': 'delete', 'oib': request.oib, 'response': _element_name(response)}


@router.get('/list')
def ams_list():
    settings = get_settings()
    try:
        client = create_ams_client(settings)
        response = client.list_participants()
        participants = parse_participant_page(response)
    except AmsError as exc:
        logger.exception('AMS list')
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        'status': 'ok',
        'publisher_id': settings.publisher_id,
        'participants': [
            {'scheme': p.scheme, 'identifier': p.identifier, 'full': p.full}
            for p in participants
        ],
    }


def _element_name(el: etree._Element) -> str:
    return etree.QName(el).localname if el is not None else ''
