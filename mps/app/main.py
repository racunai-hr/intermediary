import logging

from fastapi import FastAPI

from app.routes.admin import router as admin_router
from app.routes.mps import router as mps_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title='racunai MPS', docs_url=None, redoc_url=None)

app.include_router(mps_router)
app.include_router(admin_router)


@app.get('/health')
def health():
    return {'status': 'ok'}
