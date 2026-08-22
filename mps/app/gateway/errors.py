from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GatewayError(Exception):
    code: str
    message: str
    http_status: int
    retryable: bool = False
    request_id: str = ''

    def as_body(self) -> dict:
        return {
            'error': {
                'code': self.code,
                'message': self.message,
                'retryable': self.retryable,
                'request_id': self.request_id,
            }
        }


def invalid_request(message: str) -> GatewayError:
    return GatewayError('INVALID_REQUEST', message, 400)


def invalid_ubl(message: str) -> GatewayError:
    return GatewayError('INVALID_UBL', message, 400)


def unauthorized_subject(message: str = 'Caller is not allowed to act for this taxpayer.') -> GatewayError:
    return GatewayError('UNAUTHORIZED_SUBJECT', message, 403)


def binding_not_active(message: str = 'Inbound binding is not active.') -> GatewayError:
    return GatewayError('BINDING_NOT_ACTIVE', message, 409)


def capability_not_supported(message: str = 'Provider capability is not available.') -> GatewayError:
    return GatewayError('CAPABILITY_NOT_SUPPORTED', message, 409)


def idempotency_conflict(message: str = 'Idempotency-Key was reused with a different payload.') -> GatewayError:
    return GatewayError('IDEMPOTENCY_CONFLICT', message, 409)


def document_not_found(message: str = 'Document was not found.') -> GatewayError:
    return GatewayError('DOCUMENT_NOT_FOUND', message, 404)


def requires_review(message: str) -> GatewayError:
    return GatewayError('REQUIRES_REVIEW', message, 409)


def provider_not_configured(message: str = 'Provider credentials are not configured.') -> GatewayError:
    return GatewayError('PROVIDER_NOT_CONFIGURED', message, 409)


def provider_unavailable(message: str = 'Provider is temporarily unavailable.') -> GatewayError:
    return GatewayError('PROVIDER_UNAVAILABLE', message, 503, retryable=True)


def ambiguous_provider_result(message: str = 'Provider result is ambiguous.') -> GatewayError:
    return GatewayError('AMBIGUOUS_PROVIDER_RESULT', message, 409)


def from_super_http_error(exc: Exception) -> GatewayError:
    from app.gateway.adapters.super.client import SuperHttpError

    if not isinstance(exc, SuperHttpError):
        raise TypeError('Expected SuperHttpError.') from exc
    if exc.ambiguous:
        return ambiguous_provider_result(str(exc))
    return GatewayError(
        'PROVIDER_UNAVAILABLE',
        str(exc),
        503,
        retryable=exc.retryable,
    )
