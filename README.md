# Alpha Updated

Flask service that tags uploaded audio files (artist/album/title/cover art)
and fixes episode-style filenames.

## Requirements

- Python 3.11+
- **ffmpeg / ffprobe on PATH** — required for m4a/aac remuxing. Not installable
  via pip; on Render/Railway/Koyeb add it via a buildpack or Dockerfile apt
  step, it is **not** bundled by `requirements.txt`.

## Local run

```bash
pip install -r requirements.txt
python a.py
```

Dev server binds `0.0.0.0:$PORT` (default 5001) with `threaded=True`.

## Production

```bash
gunicorn a:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
```

(same as the `Procfile`, used by Render/Railway/Koyeb automatically)

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `MAX_UPLOAD_MB` | `100` | Hard cap on upload size |
| `CLEANUP_TTL_SECONDS` | `1800` | How long a processed file stays downloadable before the background sweep deletes it |
| `CLEANUP_INTERVAL_SECONDS` | `300` | How often the sweep runs |
| `ARTIST_NAME` / `ALBUM_NAME` | `@king75683` | Tag values written to every file |
| `COVER_IMAGE` | `alpha_updated/image_k.jpg` | Path to cover art embedded in output |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `PORT` | `5001` | Bind port (set automatically by most PaaS) |
| `FLASK_DEBUG` | `false` | Set `true` only for local dev |

## Known constraint

Upload (`/upload_one`) and download (`/download/<id>/<file>`) are separate
requests. Processed files are **not** deleted immediately after upload —
they're kept until `CLEANUP_TTL_SECONDS` expires, checked every
`CLEANUP_INTERVAL_SECONDS`. If your `CLEANUP_TTL_SECONDS` is shorter than the
realistic gap between a user uploading and clicking download, downloads will
start returning 404. Tune accordingly for your traffic pattern.

## Endpoints

- `POST /upload_one` — single file, form field `audio`
- `POST /upload` — multiple files, form field `audio` (repeated)
- `GET /download/<req_id>/<filename>`
- `GET /health` — liveness check for PaaS health checks
- `GET /check` — debug: ffmpeg/ffprobe presence, cover image, config
