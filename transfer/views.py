"""
Upload, list and download the transfer zips.

One flat directory, one file per name. Uploading a name that already exists
replaces it, so the download link for e.g. backend-zip.zip never changes.
"""
import hashlib
import os
import re
from datetime import datetime, timezone

from django.conf import settings
from django.http import (
    FileResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import blob
from .auth import require_api_key

# Deliberately strict: no directory separators, no leading dot, nothing that
# could walk out of STORAGE_DIR. Anything else is rejected rather than cleaned,
# so a surprising name fails loudly instead of being silently renamed.
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')

SHA_SUFFIX = '.sha256'
PART_SUFFIX = '.part'


def _storage_dir():
    settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return settings.STORAGE_DIR


def _reject_name(name):
    """None if the name is usable, else a JsonResponse explaining why not."""
    if not name:
        return JsonResponse({"error": "A file name is required."}, status=400)
    if '..' in name or '/' in name or '\\' in name or not SAFE_NAME.match(name):
        return JsonResponse(
            {"error": "Invalid name. Use letters, digits, dot, dash and underscore only."},
            status=400,
        )
    if name.endswith(SHA_SUFFIX) or name.endswith(PART_SUFFIX):
        return JsonResponse({"error": "That suffix is reserved."}, status=400)
    if settings.ALLOWED_EXTENSIONS and not name.lower().endswith(settings.ALLOWED_EXTENSIONS):
        allowed = ', '.join(settings.ALLOWED_EXTENSIONS)
        return JsonResponse({"error": f"Only these extensions are allowed: {allowed}"}, status=400)
    return None


def _resolved_path(name):
    """
    The path for a validated name, confirmed to sit inside STORAGE_DIR.

    SAFE_NAME already rules out traversal; this is the belt-and-braces check, so
    a future change to the pattern cannot turn into a path-traversal bug.
    """
    base = _storage_dir().resolve()
    path = (base / name).resolve()
    if path.parent != base:
        raise ValueError("resolved outside the storage directory")
    return path


def _sidecar(path):
    return path.with_name(path.name + SHA_SUFFIX)


def _read_sha(path):
    side = _sidecar(path)
    if side.exists():
        try:
            return side.read_text(encoding='ascii').strip() or None
        except OSError:
            return None
    return None


def _describe(path, request=None):
    stat = path.stat()
    info = {
        "name": path.name,
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "sha256": _read_sha(path),
    }
    if request is not None:
        info["download_url"] = request.build_absolute_uri(f"/api/download/{path.name}")
    return info


@csrf_exempt
@require_http_methods(["POST"])
@require_api_key
def upload(request):
    """
    POST /api/upload/   multipart form, field "file"

    The stored name comes from the uploaded file unless "name" is given. Writes
    to <name>.part first and renames on success, so a failed or half-finished
    upload leaves the previous good copy intact instead of truncating it.
    """
    upload_file = request.FILES.get('file')
    if not upload_file:
        return JsonResponse({"error": "No file sent. Use multipart form field 'file'."}, status=400)

    name = request.POST.get('name') or os.path.basename(upload_file.name or '')
    bad = _reject_name(name)
    if bad:
        return bad

    limit = settings.MAX_UPLOAD_BYTES
    if limit and upload_file.size and upload_file.size > limit:
        return JsonResponse(
            {"error": f"File is {upload_file.size} bytes; the limit is {limit}."},
            status=413,
        )

    # Blob first: _resolved_path() below creates the storage directory, which
    # throws on a read-only filesystem -- exactly where Blob is being used.
    if blob.is_enabled():
        # Vercel caps a function request body at 4.5 MB, so this path only ever
        # serves small files; the pack script PUTs large zips to Blob directly.
        raw = b''.join(upload_file.chunks())
        try:
            stored = blob.upload(name, raw)
        except blob.BlobError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        stored["sha256"] = hashlib.sha256(raw).hexdigest()
        stored["download_url"] = request.build_absolute_uri(f"/api/download/{name}")
        stored["replaced"] = None   # Blob does not say whether it overwrote
        return JsonResponse(stored, status=200)

    try:
        final = _resolved_path(name)
    except ValueError:
        return JsonResponse({"error": "Invalid name."}, status=400)

    part = final.with_name(final.name + PART_SUFFIX)
    digest = hashlib.sha256()
    written = 0
    try:
        with open(part, 'wb') as out:
            for chunk in upload_file.chunks():
                written += len(chunk)
                if limit and written > limit:
                    raise ValueError(f"exceeded {limit} bytes")
                digest.update(chunk)
                out.write(chunk)
        existed = final.exists()
        # Atomic on the same filesystem, and os.replace overwrites on Windows
        # too, which os.rename does not.
        os.replace(part, final)
        _sidecar(final).write_text(digest.hexdigest(), encoding='ascii')
    except ValueError as exc:
        part.unlink(missing_ok=True)
        return JsonResponse({"error": str(exc)}, status=413)
    except OSError as exc:
        part.unlink(missing_ok=True)
        return JsonResponse({"error": f"Could not store the file: {exc}"}, status=500)

    body = _describe(final, request)
    body["replaced"] = existed
    return JsonResponse(body, status=200)


@require_http_methods(["GET"])
@require_api_key
def listing(request):
    """GET /api/files/ - newest first, so a client can pick the latest zip."""
    if blob.is_enabled():
        try:
            found = blob.listing()
        except blob.BlobError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        files = []
        for f in found:
            if not f["name"] or _reject_name(f["name"]) is not None:
                continue
            f["download_url"] = request.build_absolute_uri(f"/api/download/{f['name']}")
            files.append(f)
        return JsonResponse({"count": len(files), "files": files})

    files = []
    for path in sorted(_storage_dir().glob('*')):
        if not path.is_file():
            continue
        # Anything the download endpoint would refuse must not be advertised
        # here: .gitkeep was being listed with a download_url that 400s, because
        # _reject_name turns away leading dots and foreign extensions.
        if _reject_name(path.name) is not None:
            continue
        if path.name.endswith(SHA_SUFFIX) or path.name.endswith(PART_SUFFIX):
            continue
        files.append(_describe(path, request))
    files.sort(key=lambda f: f["modified"], reverse=True)
    return JsonResponse({"count": len(files), "files": files})


@require_http_methods(["GET"])
@require_api_key
def download(request, name):
    """GET /api/download/<name> - streams the file, named as it was uploaded."""
    bad = _reject_name(name)
    if bad:
        return bad
    if blob.is_enabled():
        try:
            found = blob.find(name)
        except blob.BlobError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        if not found:
            return JsonResponse({"error": f"No such file: {name}"}, status=404)

        target = found["download_url"]
        if not blob.is_private(target):
            # Public store: redirect, so the bytes never pass through the
            # function. The URL is public but unguessable.
            return HttpResponseRedirect(target)

        # Private store: an unauthenticated GET there answers 403, so the
        # redirect would be useless and we have to relay the bytes. Fine for
        # modest files; a large zip risks the platform's execution limit, which
        # is why the pack script fetches straight from the store with the token
        # instead of coming through here.
        try:
            stream, length = blob.open_stream(target)
        except blob.BlobError as exc:
            return JsonResponse({"error": str(exc)}, status=502)

        response = StreamingHttpResponse(
            iter(lambda: stream.read(256 * 1024), b''),
            content_type='application/zip',
        )
        response['Content-Disposition'] = f'attachment; filename="{name}"'
        if length:
            response['Content-Length'] = length
        return response

    try:
        path = _resolved_path(name)
    except ValueError:
        return JsonResponse({"error": "Invalid name."}, status=400)
    if not path.is_file():
        return JsonResponse({"error": f"No such file: {name}"}, status=404)

    response = FileResponse(open(path, 'rb'), as_attachment=True, filename=path.name)
    response['Content-Length'] = path.stat().st_size
    sha = _read_sha(path)
    if sha:
        # Lets the caller verify the transfer without a second request.
        response['X-Content-SHA256'] = sha
    return response


@csrf_exempt
@require_http_methods(["DELETE"])
@require_api_key
def delete(request, name):
    """DELETE /api/files/<name>"""
    bad = _reject_name(name)
    if bad:
        return bad
    if blob.is_enabled():
        try:
            found = blob.find(name)
            if not found:
                return JsonResponse({"error": f"No such file: {name}"}, status=404)
            blob.delete(found["url"])
        except blob.BlobError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        return JsonResponse({"deleted": name})

    try:
        path = _resolved_path(name)
    except ValueError:
        return JsonResponse({"error": "Invalid name."}, status=400)
    if not path.is_file():
        return JsonResponse({"error": f"No such file: {name}"}, status=404)
    path.unlink()
    _sidecar(path).unlink(missing_ok=True)
    return JsonResponse({"deleted": name})


@require_http_methods(["GET"])
def health(request):
    """GET /api/health/ - unauthenticated, for a platform health check."""
    info = {
        "ok": True,
        "backend": settings.STORAGE_BACKEND,
        "configured": bool(settings.API_KEY and settings.API_SECRET),
    }
    if blob.is_enabled():
        info["blob_token_set"] = bool(settings.BLOB_READ_WRITE_TOKEN)
        try:
            found = blob.listing()
            info["stored"] = len(found)
            info["storage_writable"] = True
            if found:
                info["store_private"] = blob.is_private(found[0].get("url"))
        except blob.BlobError as exc:
            info["ok"] = False
            info["storage_writable"] = False
            info["error"] = str(exc)
    else:
        info["storage_dir"] = str(settings.STORAGE_DIR)
        try:
            info["storage_writable"] = os.access(str(_storage_dir()), os.W_OK)
        except OSError:
            # mkdir itself fails on a read-only filesystem.
            info["storage_writable"] = False
        if not info["storage_writable"]:
            # The common case is a serverless deploy with no Blob store linked.
            # Saying so here saves guessing why uploads return 500.
            info["ok"] = False
            info["hint"] = (
                "Storage is not writable and BLOB_READ_WRITE_TOKEN is not set, so "
                "the backend fell back to 'disk'. Connect a Blob store to the "
                "project and REDEPLOY - environment variables only reach new "
                "deployments, so connecting a store does not affect one already "
                "running."
            )
    return JsonResponse(info)
