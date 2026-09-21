from __future__ import annotations

from tests.conftest import auth_header, requires_postgres


@requires_postgres
def test_pondi_capabilities_true_without_credential(client):
    response = client.get('/v1/providers/pondi/capabilities', headers=auth_header(scope='gateway.read'))
    assert response.status_code == 200
    body = response.json()
    assert body['supports']['outbound_send'] is True
    assert body['outbound_readiness']['ready'] is False
    assert body['inbound_readiness']['active_binding'] is False
