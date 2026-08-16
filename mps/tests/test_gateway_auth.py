import jwt
import pytest

from app.gateway.auth import decode_principal
from app.gateway.canonical import validate_amount
from app.gateway.errors import GatewayError
from app.gateway.settings import get_gateway_settings
from tests.conftest import make_token


@pytest.fixture(autouse=True)
def _jwt_settings(monkeypatch):
    monkeypatch.setenv('GATEWAY_DATABASE_URL', 'postgresql+psycopg://x:x@postgis:5432/x')
    monkeypatch.setenv('GATEWAY_JWT_SECRET', 'test-gateway-secret')
    get_gateway_settings.cache_clear()
    yield
    get_gateway_settings.cache_clear()



def test_amount_rejects_float_exponent_and_negative():
    with pytest.raises(GatewayError):
        validate_amount(1.23)  # type: ignore[arg-type]
    with pytest.raises(GatewayError):
        validate_amount('1e2')
    with pytest.raises(GatewayError):
        validate_amount('-1.00')
    with pytest.raises(GatewayError):
        validate_amount('1.234')
    assert validate_amount('123.45') == '123.45'


def test_jwt_rejects_wrong_algorithm(monkeypatch):
    monkeypatch.setenv('GATEWAY_JWT_SECRET', 'test-gateway-secret')
    token = jwt.encode(
        {
            'iss': 'racunai-api',
            'aud': 'racunai-intermediary',
            'exp': 9999999999,
            'jti': 'x',
            'sub': 'racunai-api',
            'scope': 'gateway.read',
            'taxpayers': ['*'],
        },
        'test-gateway-secret',
        algorithm='HS384',
    )
    with pytest.raises(GatewayError):
        decode_principal(f'Bearer {token}')


def test_jwt_rejects_wrong_issuer():
    token = make_token(iss='other-iss')
    with pytest.raises(GatewayError):
        decode_principal(f'Bearer {token}')


def test_jwt_rejects_wrong_audience():
    token = make_token(aud='other-aud')
    with pytest.raises(GatewayError):
        decode_principal(f'Bearer {token}')
