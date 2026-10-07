"""
Static key/secret authentication.

Both values are compared with hmac.compare_digest so a wrong key cannot be
recovered by timing the response, and a failure never says which half was wrong.
"""
import hmac
from functools import wraps

from django.conf import settings
from django.http import JsonResponse

KEY_HEADER = 'HTTP_X_API_KEY'
SECRET_HEADER = 'HTTP_X_API_SECRET'


def _matches(sent, expected):
    if not sent or not expected:
        return False
    return hmac.compare_digest(str(sent), str(expected))


def require_api_key(view):
    """Reject the request unless both X-Api-Key and X-Api-Secret are correct."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.API_KEY or not settings.API_SECRET:
            return JsonResponse(
                {"error": "Server is missing FILEDROP_API_KEY / FILEDROP_API_SECRET."},
                status=500,
            )

        key_ok = _matches(request.META.get(KEY_HEADER), settings.API_KEY)
        secret_ok = _matches(request.META.get(SECRET_HEADER), settings.API_SECRET)

        # Both are evaluated before branching, so the response time does not
        # reveal that the key matched but the secret did not.
        if not (key_ok and secret_ok):
            return JsonResponse(
                {"error": "Unauthorized. Send X-Api-Key and X-Api-Secret headers."},
                status=401,
            )
        return view(request, *args, **kwargs)

    return wrapper
