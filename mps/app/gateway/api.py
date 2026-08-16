from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.gateway.errors import GatewayError
from app.gateway.routes.v1 import router as v1_router
from app.gateway.settings import get_gateway_settings


def register_gateway(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        request_id = request.headers.get('X-Request-Id', '')
        exc.request_id = exc.request_id or request_id
        return JSONResponse(status_code=exc.http_status, content=exc.as_body())

    app.include_router(v1_router)


def boot_gateway() -> None:
    get_gateway_settings()
    from app.gateway.db import check_database

    check_database()
