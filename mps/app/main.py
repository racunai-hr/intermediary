import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.gateway.api import boot_gateway, register_gateway
from app.routes.admin import router as admin_router
from app.routes.mps import router as mps_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    boot_gateway()
    yield


app = FastAPI(title='racunai Fiscal Gateway', docs_url=None, redoc_url=None, lifespan=lifespan)
register_gateway(app)
app.include_router(mps_router)
app.include_router(admin_router)


@app.get('/health')
def health():
    return {'status': 'ok'}
