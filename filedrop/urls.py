from django.http import JsonResponse
from django.urls import include, path


def root(request):
    """Nothing useful at / - point people at the health check."""
    return JsonResponse({
        "service": "filedrop",
        "endpoints": [
            "POST   /api/upload/            multipart field 'file'",
            "GET    /api/files/             list what is stored",
            "GET    /api/download/<name>    fetch one file",
            "DELETE /api/files/<name>       remove one file",
            "GET    /api/health/",
        ],
        "auth": "X-Api-Key and X-Api-Secret headers on everything except /api/health/",
    })


urlpatterns = [
    path('', root),
    path('api/', include('transfer.urls')),
]
