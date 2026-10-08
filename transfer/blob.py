"""
Vercel Blob REST client.

Why this exists: Vercel's filesystem is read-only apart from an ephemeral /tmp,
so the disk backend cannot work there. Blob is the store; this module is the only
place that knows its wire format.

Deliberately stdlib-only (urllib) so the service keeps no extra dependency.

Two things about Blob that shape the design:

  * Blob URLs are PUBLIC. They are long and unguessable, but anyone holding one
    can download without credentials. The key/secret pair guards the listing and
    the redirect, not the bytes themselves.
  * A function request body is capped at 4.5 MB on Vercel, so a large upload must
    go from the client straight to Blob and never through Django. upload() here
    is for small files and for callers that are not the pack script.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from django.conf import settings

BASE = 'https://blob.vercel-storage.com'


class BlobError(RuntimeError):
    """A Blob API call failed. The message carries the API's own response."""


def is_enabled():
    return settings.STORAGE_BACKEND == 'blob'


def _token():
    token = settings.BLOB_READ_WRITE_TOKEN
    if not token:
        raise BlobError(
            "BLOB_READ_WRITE_TOKEN is not set. Link a Blob store to the project "
            "in the Vercel dashboard, which sets it automatically."
        )
    return token


def _headers(extra=None):
    headers = {
        'authorization': f'Bearer {_token()}',
        # Required by the API; bump via FILEDROP_BLOB_API_VERSION if Vercel moves on.
        'x-api-version': settings.BLOB_API_VERSION,
    }
    if extra:
        headers.update(extra)
    return headers


def _request(method, url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, str(v))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:500]
        raise BlobError(f"Blob API {method} returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BlobError(f"Could not reach the Blob API: {exc.reason}") from exc
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        raise BlobError(f"Blob API returned non-JSON: {body[:200]!r}")


def _as_file(blob):
    """A Blob API entry in the same shape the disk backend returns."""
    uploaded = blob.get('uploadedAt')
    if uploaded:
        # The API sends ISO-8601 with a trailing Z, which fromisoformat rejects
        # before Python 3.11.
        try:
            modified = datetime.fromisoformat(uploaded.replace('Z', '+00:00')).isoformat()
        except ValueError:
            modified = uploaded
    else:
        modified = datetime.now(tz=timezone.utc).isoformat()
    return {
        "name": blob.get('pathname'),
        "size": blob.get('size'),
        "modified": modified,
        # Set by the uploader; Blob itself does not hash for us.
        "sha256": None,
        "url": blob.get('url'),
        "download_url": blob.get('downloadUrl') or blob.get('url'),
    }


def upload(name, data, content_type='application/zip'):
    """
    PUT bytes at `name`, replacing whatever was there.

    `data` may be bytes or a file-like object. On Vercel this is only usable
    below the 4.5 MB request cap -- the pack script uploads direct instead, with
    exactly the same headers.
    """
    url = f'{BASE}/{urllib.parse.quote(name)}'
    headers = _headers({
        'x-content-type': content_type,
        # Keep the pathname exactly as given, so re-uploading backend-zip.zip
        # replaces it instead of creating backend-zip-<random>.zip and leaving
        # the download link pointing at a stale copy.
        'x-add-random-suffix': '0',
        'x-allow-overwrite': '1',
        # No CDN caching: the whole point is that the newest upload wins.
        'x-cache-control-max-age': '0',
    })
    if hasattr(data, 'read'):
        data = data.read()
    headers['content-length'] = len(data)
    return _as_file(_request('PUT', url, data=data, headers=headers))


def listing(prefix=''):
    """Every blob in the store, newest first."""
    files = []
    cursor = None
    while True:
        params = {'limit': '1000'}
        if prefix:
            params['prefix'] = prefix
        if cursor:
            params['cursor'] = cursor
        body = _request('GET', f'{BASE}?{urllib.parse.urlencode(params)}', headers=_headers())
        for blob in body.get('blobs', []):
            files.append(_as_file(blob))
        cursor = body.get('cursor')
        if not body.get('hasMore') or not cursor:
            break
    files.sort(key=lambda f: f["modified"] or '', reverse=True)
    return files


def find(name):
    """One blob by exact pathname, or None."""
    for blob in listing(prefix=name):
        if blob["name"] == name:
            return blob
    return None


def is_private(url):
    """
    True for a private store's host, e.g.
    tlpyy7tbc5ptcdau.private.blob.vercel-storage.com

    A private blob answers an unauthenticated GET with 403, so redirecting a
    caller there would simply fail. The host name is the only signal the API
    gives us without an extra round trip.
    """
    return '.private.blob.vercel-storage.com' in (url or '')


def open_stream(url, timeout=300):
    """
    The blob's bytes as a file-like object, authenticated.

    Used to proxy a private blob, since the caller cannot fetch it directly.
    Returns (stream, length) -- length is None when the store does not say.
    """
    req = urllib.request.Request(url, method='GET')
    req.add_header('authorization', f'Bearer {_token()}')
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:300]
        raise BlobError(f"Blob GET returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BlobError(f"Could not reach the blob store: {exc.reason}") from exc
    length = resp.headers.get('content-length')
    return resp, (int(length) if length and length.isdigit() else None)


def delete(url):
    _request(
        'POST',
        f'{BASE}/delete',
        data=json.dumps({"urls": [url]}).encode('utf-8'),
        headers=_headers({'content-type': 'application/json'}),
    )
