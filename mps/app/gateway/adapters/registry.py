from __future__ import annotations

from app.gateway.adapters.base import UnimplementedAdapter
from app.gateway.adapters.super.adapter import SuperAdapter


def get_adapter(provider: str):
    if provider == 'super':
        return SuperAdapter()
    return UnimplementedAdapter()
