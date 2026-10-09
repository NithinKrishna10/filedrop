from django.urls import path, re_path

from . import relay, views

# The name pattern mirrors views.SAFE_NAME, so a bad name is a 404 from the
# router rather than reaching a view.
NAME = r'(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,99})'

urlpatterns = [
    path('health/', views.health, name='health'),
    path('upload/', views.upload, name='upload'),
    path('files/', views.listing, name='listing'),
    re_path(rf'^download/{NAME}$', views.download, name='download'),
    re_path(rf'^files/{NAME}$', views.delete, name='delete'),

    # Phone scan relay for the CISF Scanner Simulator extension (see relay.py)
    path('scan/start/', relay.start, name='scan-start'),
    re_path(rf'^scan/(?P<session>{relay.SESSION_RE})/push$', relay.push, name='scan-push'),
    re_path(rf'^scan/(?P<session>{relay.SESSION_RE})/poll$', relay.poll, name='scan-poll'),
]
