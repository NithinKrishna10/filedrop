from django.http import HttpResponseRedirect, JsonResponse
from django.urls import include, path, re_path

from transfer import relay


def root(request):
    """Nothing useful at / - point people at the health check."""
    # A person opening the bare address in a browser (usually the phone) wants
    # the scanner page, not this JSON. Scripts don't ask for text/html.
    if 'text/html' in request.META.get('HTTP_ACCEPT', ''):
        return HttpResponseRedirect('/scan/')
    return JsonResponse({
        "service": "filedrop",
        "endpoints": [
            "POST   /api/upload/            multipart field 'file'",
            "GET    /api/files/             list what is stored",
            "GET    /api/download/<name>    fetch one file",
            "DELETE /api/files/<name>       remove one file",
            "GET    /api/health/",
            "POST   /api/scan/start/        phone scan relay (CISF scanner extension)",
        ],
        "auth": "X-Api-Key and X-Api-Secret headers on everything except /api/health/",
    })


urlpatterns = [
    path('', root),
    path('api/', include('transfer.urls')),
    # The page a phone opens from the extension's QR, and its QR decoder
    path('scan/', relay.pair_page, name='scan-pair'),
    path('scan/jsQR.js', relay.phone_jsqr, name='scan-jsqr'),
    re_path(rf'^scan/(?P<session>{relay.SESSION_RE})$', relay.phone_page, name='scan-page'),
]
