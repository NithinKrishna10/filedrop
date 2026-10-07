"""
Settings for filedrop - a one-bucket file relay for the pack/unpack scripts.

No database: DATABASES is deliberately empty and none of the contrib apps that
need one are installed. Files live on disk, named after the zip, and an upload
replaces whatever was there before.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"{name} must be set")
    return value


# SECRET_KEY is unused (no sessions, no signing) but Django insists on one.
SECRET_KEY = _env('DJANGO_SECRET_KEY', 'filedrop-unused-no-sessions')
DEBUG = _env('DJANGO_DEBUG', '0') == '1'

# Comma-separated, e.g. "filedrop.onrender.com". '*' is the default because the
# only thing protecting this service is the key/secret pair, not the hostname.
ALLOWED_HOSTS = [h.strip() for h in _env('DJANGO_ALLOWED_HOSTS', '*').split(',') if h.strip()]

# Needed on any platform that terminates TLS in front of the app, or Django
# builds http:// download URLs behind an https:// proxy.
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = []          # nothing here needs the ORM, admin or templates
MIDDLEWARE = []              # auth is per-view; no sessions, no CSRF cookie
DATABASES = {}               # explicitly none

ROOT_URLCONF = 'filedrop.urls'
WSGI_APPLICATION = 'filedrop.wsgi.application'

USE_TZ = True
TIME_ZONE = 'Asia/Kolkata'

# ── filedrop ─────────────────────────────────────────────────────────────────

# Where the zips land. Override on a host whose project directory is read-only
# or wiped between deploys -- point it at a mounted disk instead.
STORAGE_DIR = Path(_env('FILEDROP_STORAGE_DIR', str(BASE_DIR / 'storage')))

# The static credentials. Both must be sent on every request.
API_KEY = _env('FILEDROP_API_KEY', required=not DEBUG)
API_SECRET = _env('FILEDROP_API_SECRET', required=not DEBUG)

# Reject anything larger, before reading the body. 0 disables the check.
MAX_UPLOAD_BYTES = int(_env('FILEDROP_MAX_UPLOAD_BYTES', str(512 * 1024 * 1024)))

# Only these extensions may be uploaded or served. Empty allows any.
ALLOWED_EXTENSIONS = tuple(
    e.strip().lower() for e in _env('FILEDROP_ALLOWED_EXT', '.zip').split(',') if e.strip()
)

# Spool to a temp file past this size rather than holding the upload in memory,
# so a 40 MB zip does not cost 40 MB of RAM.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
# Applies to non-file form fields only, but keep it small so a junk body cannot
# be used to chew memory.
DATA_UPLOAD_MAX_MEMORY_SIZE = 1 * 1024 * 1024
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
