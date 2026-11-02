from __future__ import annotations

from app.gateway.adapters.base import UnimplementedAdapter
from app.gateway.adapters.pondi.adapter import PondiAdapter
from app.gateway.adapters.super.adapter import SuperAdapter


def get_adapter(provider: str):
    if provider == 'super':
        return SuperAdapter()
    if provider == 'pondi':
        return PondiAdapter()
    return UnimplementedAdapter()


def adapter_is_live(adapter) -> bool:
    return not isinstance(adapter, UnimplementedAdapter) and hasattr(adapter, 'send')
