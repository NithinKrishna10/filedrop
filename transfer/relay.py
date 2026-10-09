"""
Phone scan relay for the CISF Scanner Simulator extension.

The extension starts a session and shows a QR of the phone page. The phone scans
permit QRs with its camera and pushes each value here; the extension polls for
them and types them into the CISF page like a USB scanner would.

There is no database and, on Vercel, no shared memory between function
instances, so every scan is its own small file: scans/<rand>/<n>.json, n = 1, 2,
3... The poller knows the next n, so it reads one exact path and never needs a
listing (a Blob listing is an "advanced" operation; a read by path is not).

Sessions need no storage either: the id is signed with FILEDROP_API_SECRET and
carries its issue time, so any instance can check it without a lookup.
"""
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt

from . import blob
from .auth import require_api_key

SESSION_TTL = 12 * 60 * 60      # a session works for a whole testing day
PAIR_TTL = 30 * 60              # the 6-digit code typed on the phone is short-lived
MAX_VALUE_LEN = 500             # permit codes and UUIDs are far shorter
MAX_BATCH = 10                  # scans returned per poll
PROBE_LIMIT = 50                # free slots tried when the phone's counter is stale
PHONE_DIR = Path(__file__).resolve().parent / 'phone'

# Mirrors the session format below; also used by the URL patterns.
SESSION_RE = r'[A-Za-z0-9_-]{16}\.[0-9a-z]{1,10}\.[0-9a-f]{16}'


# ── Sessions ──────────────────────────────────────────────────────────────────

def _sign(rand, issued):
    msg = f'scan:{rand}:{issued}'.encode()
    return hmac.new(str(settings.API_SECRET).encode(), msg, hashlib.sha256).hexdigest()[:16]


def _new_session():
    rand = secrets.token_urlsafe(12)            # 16 chars of [A-Za-z0-9_-]
    issued = _base36(int(time.time()))
    return f'{rand}.{issued}.{_sign(rand, issued)}'


def _base36(n):
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def _check_session(session):
    """The session's storage key if it is genuine and not expired, else None."""
    try:
        rand, issued, sig = session.split('.')
        age = time.time() - int(issued, 36)
    except ValueError:
        return None
    if not settings.API_SECRET or not hmac.compare_digest(sig, _sign(rand, issued)):
        return None
    if age < -300 or age > SESSION_TTL:
        return None
    return rand


def session_view(view):
    """Resolve the <session> URL part to its storage key, or answer 403."""

    @wraps(view)
    def wrapper(request, session, *args, **kwargs):
        key = _check_session(session)
        if key is None:
            return JsonResponse(
                {"error": "This scan session is invalid or has expired. Start a new one from the extension."},
                status=403,
            )
        return view(request, key, *args, **kwargs)

    return wrapper


# ── CORS: the extension page and the phone page call these from other origins ──

def cors(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method == 'OPTIONS':
            response = HttpResponse(status=204)
        else:
            response = view(request, *args, **kwargs)
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Content-Type, X-Api-Key, X-Api-Secret'
        response['Access-Control-Max-Age'] = '600'
        response['Cache-Control'] = 'no-store'
        return response

    return wrapper


# ── Storage: one tiny JSON file per scan ─────────────────────────────────────

_blob_host = None   # learnt from the first PUT on this instance


def _path(key, n):
    return f'scans/{key}/{n}.json'


def _pair_path(code):
    return f'scans/pair/{code}.json'


def _derived_blob_host():
    """
    <storeid>.<access>.blob.vercel-storage.com, from the read-write token
    (vercel_blob_rw_<storeId>_<secret>), the same way @vercel/blob builds URLs.
    """
    parts = (settings.BLOB_READ_WRITE_TOKEN or '').split('_')
    if len(parts) < 5 or not parts[3]:
        return None
    return f'{parts[3].lower()}.{settings.BLOB_ACCESS}.blob.vercel-storage.com'


def _put(key, n, record):
    _write(_path(key, n), record)


def _get(key, n):
    """The stored record, or None if slot n is still empty."""
    return _read(_path(key, n))


def _write(rel, record):
    global _blob_host
    data = json.dumps(record).encode('utf-8')
    if blob.is_enabled():
        stored = blob.upload(rel, data, content_type='application/json')
        host = urllib.parse.urlsplit(stored.get('url') or '').hostname
        if host:
            _blob_host = host
        return
    path = settings.STORAGE_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.part')
    tmp.write_bytes(data)
    tmp.replace(path)


def _read(rel):
    """The JSON stored at rel, or None if there is nothing there yet."""
    if blob.is_enabled():
        host = _blob_host or _derived_blob_host()
        if not host:
            raise blob.BlobError("Could not work out the Blob store's host name.")
        # The query string only defeats any cached "not found" for this path.
        url = f'https://{host}/{rel}?t={time.time_ns()}'
        req = urllib.request.Request(url, method='GET')
        req.add_header('authorization', f'Bearer {settings.BLOB_READ_WRITE_TOKEN}')
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise blob.BlobError(f"Blob GET returned {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise blob.BlobError(f"Could not reach the blob store: {exc.reason}") from exc
        except ValueError:
            return None
    path = settings.STORAGE_DIR / rel
    try:
        return json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        return None


def _body(request):
    try:
        return json.loads(request.body or b'{}')
    except ValueError:
        return {}


def _int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


# ── API ───────────────────────────────────────────────────────────────────────

@csrf_exempt
@cors
@require_api_key
def start(request):
    """
    POST /api/scan/start/   (X-Api-Key / X-Api-Secret)

    A new session, and the page the phone should open. Slot 0 is written as a
    marker, which also confirms storage works before anyone starts scanning.
    """
    if request.method != 'POST':
        return JsonResponse({"error": "Use POST."}, status=405)
    session = _new_session()
    key = _check_session(session)
    code = f'{secrets.randbelow(10 ** 6):06d}'
    try:
        _put(key, 0, {"type": "start", "at": time.time()})
        _write(_pair_path(code), {"session": session, "at": time.time()})
    except (blob.BlobError, OSError) as exc:
        return JsonResponse({"error": f"Could not start a session: {exc}"}, status=502)
    return JsonResponse({
        "session": session,
        "code": code,
        "phone_url": request.build_absolute_uri(f'/scan/{session}'),
        "pair_url": request.build_absolute_uri('/scan/'),
        "expires_in": SESSION_TTL,
    })


@csrf_exempt
@cors
@session_view
def push(request, key):
    """
    POST /api/scan/<session>/push   {"value": "...", "next": <phone's counter>}

    Called by the phone. "next" is only a hint: if that slot is taken (the page
    was reloaded, or two phones share a session) the next free one is used.
    {"type": "hello"} instead of a value tells the extension a phone connected.
    """
    if request.method != 'POST':
        return JsonResponse({"error": "Use POST."}, status=405)
    data = _body(request)
    kind = 'hello' if data.get('type') == 'hello' else 'scan'
    value = str(data.get('value') or '').strip()
    if kind == 'scan' and not value:
        return JsonResponse({"error": "Nothing to send."}, status=400)
    if len(value) > MAX_VALUE_LEN:
        return JsonResponse({"error": "That value is too long to be a permit code."}, status=400)

    n = max(1, _int(data.get('next'), 1))
    try:
        # The poller stops at the first empty slot, so a hint that runs ahead of
        # the stored scans would leave a gap it never reads past. Start over then.
        if n > 1 and _get(key, n - 1) is None:
            n = 1
        for _ in range(PROBE_LIMIT):
            if _get(key, n) is None:
                _put(key, n, {"type": kind, "value": value, "at": time.time(),
                              "device": request.META.get('HTTP_USER_AGENT', '')[:120]})
                return JsonResponse({"n": n, "next": n + 1})
            n += 1
    except (blob.BlobError, OSError) as exc:
        return JsonResponse({"error": f"Could not save the scan: {exc}"}, status=502)
    return JsonResponse({"error": "Too many scans in this session. Start a new one."}, status=409)


@cors
@session_view
def poll(request, key):
    """
    GET /api/scan/<session>/poll?after=<last n seen>

    Called by the extension. Returns whatever arrived after `after`, in order.
    One read per empty poll, so polling every second or two stays cheap.
    """
    after = _int(request.GET.get('after'))
    items = []
    try:
        n = after + 1
        while len(items) < MAX_BATCH:
            record = _get(key, n)
            if record is None:
                break
            items.append({"n": n, **{k: record.get(k) for k in ('type', 'value', 'at', 'device')}})
            n += 1
    except (blob.BlobError, OSError) as exc:
        return JsonResponse({"error": str(exc), "items": items}, status=502)
    last = items[-1]["n"] if items else after
    return JsonResponse({"items": items, "last": last})


# ── Phone page ────────────────────────────────────────────────────────────────

def phone_page(request, session):
    """GET /scan/<session> - the camera page the phone opens from the QR."""
    if _check_session(session) is None:
        html = ("<!doctype html><meta name=viewport content='width=device-width'>"
                "<body style='font:16px system-ui;padding:24px'><h2>Session expired</h2>"
                "<p>Start a new phone scan from the extension and scan its QR again.</p>")
        return HttpResponse(html, status=403)
    html = (PHONE_DIR / 'scan.html').read_text(encoding='utf-8')
    html = html.replace('__SESSION__', session)
    response = HttpResponse(html, content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


PAIR_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#2d2b55"><title>CISF Phone Scanner</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #0f0e1f; color: #f3f4f6; font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  form { width: min(92vw, 360px); text-align: center; }
  h1 { font-size: 20px; margin: 0 0 6px; }
  p { color: #9ca3af; font-size: 14px; margin: 0 0 20px; }
  input { width: 100%; box-sizing: border-box; padding: 14px; font: 600 28px ui-monospace, Consolas, monospace;
          letter-spacing: .35em; text-align: center; border-radius: 14px; border: 1px solid #374151;
          background: #1b1a35; color: #fff; }
  button { width: 100%; margin-top: 12px; padding: 14px; font: 600 16px system-ui, sans-serif; border: 0;
           border-radius: 14px; background: #4ea5e0; color: #fff; }
  .err { color: #f87171; margin: 12px 0 0; font-size: 14px; }
</style></head><body>
<form method="get" action="/scan/">
  <h1>CISF Phone Scanner</h1>
  <p>Enter the 6-digit code shown in the extension window on the gate PC.</p>
  <input name="code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code"
         placeholder="000000" value="__CODE__" required autofocus>
  <button type="submit">Connect</button>
  __ERROR__
</form></body></html>"""


def pair_page(request):
    """
    GET /scan/  and  GET /scan/?code=123456

    A fixed address to type on the phone. The code (shown in the extension
    window) is swapped for that session's camera page.
    """
    code = (request.GET.get('code') or '').strip().replace(' ', '')
    error = ''
    if code:
        record = None
        if len(code) == 6 and code.isdigit():
            try:
                record = _read(_pair_path(code))
            except (blob.BlobError, OSError):
                error = 'The server could not check that code. Try again.'
        fresh = record and time.time() - float(record.get('at') or 0) < PAIR_TTL
        session = record.get('session') if fresh else None
        if session and _check_session(session):
            return HttpResponseRedirect(f'/scan/{session}')
        error = error or 'That code is wrong or has expired. Check the extension window, or click New session there.'
    html = PAIR_HTML.replace('__CODE__', escape(code)).replace(
        '__ERROR__', f'<p class="err">{escape(error)}</p>' if error else '')
    response = HttpResponse(html, status=400 if error else 200)
    response['Cache-Control'] = 'no-store'
    return response


def phone_jsqr(request):
    """GET /scan/jsQR.js - QR decoder for phones without BarcodeDetector (iPhone)."""
    response = HttpResponse((PHONE_DIR / 'jsQR.js').read_bytes(), content_type='text/javascript')
    response['Cache-Control'] = 'public, max-age=86400'
    return response
