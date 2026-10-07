# filedrop

A small Django service that holds the transfer zips so `pack` on one PC and
`unpack` on the other can talk through it instead of a USB stick.

No database. Files sit in one flat directory, named after the zip. Uploading a
name that already exists **replaces** it, so the download link for
`backend-zip.zip` never changes.

## Endpoints

Everything except `/api/health/` needs both headers:

```
X-Api-Key:    <FILEDROP_API_KEY>
X-Api-Secret: <FILEDROP_API_SECRET>
```

| | |
|---|---|
| `POST /api/upload/` | multipart field `file`; optional `name` to override the stored name |
| `GET /api/files/` | what is stored, newest first |
| `GET /api/download/<name>` | streams the file |
| `DELETE /api/files/<name>` | removes it |
| `GET /api/health/` | no auth; reports whether the storage dir is writable |

Upload and download both carry a **sha256** — on the upload response, in the
listing, and as `X-Content-SHA256` on the download — so a transfer can be
verified without a second request.

## Run locally

```bash
pip install -r requirements.txt
export FILEDROP_API_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
export FILEDROP_API_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
python manage.py runserver
```

```bash
curl -H "X-Api-Key: $FILEDROP_API_KEY" -H "X-Api-Secret: $FILEDROP_API_SECRET" \
     -F "file=@../_transfer/backend-zip.zip" \
     http://127.0.0.1:8000/api/upload/

curl -H "X-Api-Key: $FILEDROP_API_KEY" -H "X-Api-Secret: $FILEDROP_API_SECRET" \
     -O http://127.0.0.1:8000/api/download/backend-zip.zip
```

## Settings

All via environment; see `.env.example`.

| | |
|---|---|
| `FILEDROP_API_KEY` | required |
| `FILEDROP_API_SECRET` | required |
| `FILEDROP_STORAGE_DIR` | default `./storage` |
| `FILEDROP_MAX_UPLOAD_BYTES` | default 512 MB; `0` disables |
| `FILEDROP_ALLOWED_EXT` | default `.zip`; empty allows any |
| `DJANGO_ALLOWED_HOSTS` | default `*` |

## Deploying

### Vercel will not work for this

Two reasons, both hard limits rather than configuration:

1. The filesystem is **read-only** apart from `/tmp`, and `/tmp` does not
   survive between invocations. There is nowhere to keep the files.
2. Serverless functions cap the request body at **4.5 MB**. `frontend-zip.zip`
   is about 40 MB.

`vercel.json` is included so you can confirm this for yourself, but uploads will
fail. To use Vercel you would have to keep the bytes in Vercel Blob (or S3/R2)
rather than on disk — a different service to the one described here, and happy
to write that version instead.

### What does work

Anything with a real filesystem. **Render** free tier as an example:

- Build: `pip install -r requirements.txt`
- Start: `gunicorn filedrop.wsgi:application`
- Env: `FILEDROP_API_KEY`, `FILEDROP_API_SECRET`, `DJANGO_ALLOWED_HOSTS=<your-host>`
- Add a **disk** and set `FILEDROP_STORAGE_DIR` to its mount path, e.g. `/data`

Without a mounted disk the files are lost on every redeploy, which is survivable
for a transfer relay but surprising when it happens. Railway, Fly and any VPS
work the same way.

Two notes on hosted free tiers: they idle the service out, so the first request
after a quiet period takes a few seconds, and some impose their own request-body
limit — check it against your largest zip before relying on it.

## Security

The key and secret are compared with `hmac.compare_digest`, and a failure never
says which half was wrong. There are no accounts and no rate limiting: anyone
with the pair can read and overwrite every file, so treat them as a shared
password and serve only over HTTPS.

File names are **rejected** rather than sanitised — letters, digits, dot, dash
and underscore only, no leading dot, and the resolved path is re-checked against
the storage directory before any read or write.

Uploads land in `<name>.part` and are renamed over the target with `os.replace`
only once fully written, so an interrupted or over-size upload leaves the
previous good copy intact instead of truncating it.
