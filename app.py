import os
import re
import time
import uuid
import shutil
import logging
import tempfile
import threading
import subprocess

from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, send_file, abort
from flask.wrappers import Request as _FlaskRequest

from mutagen import File
from mutagen.id3 import APIC, TPE1, TALB, TIT2

# ====================================
# CONFIG (env-overridable)
# ====================================

MAX_UPLOAD_MB   = int(os.environ.get("MAX_UPLOAD_MB", "100"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
CLEANUP_TTL_SECONDS = int(os.environ.get("CLEANUP_TTL_SECONDS", str(30 * 60)))  # 30 min
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "300"))  # 5 min

ARTIST_NAME = os.environ.get("ARTIST_NAME", "@king75683")
ALBUM_NAME  = os.environ.get("ALBUM_NAME", "@king75683")
COVER_IMAGE = os.environ.get("COVER_IMAGE", "alpha_updated/image_k.jpg")

ALLOWED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}

# Magic-byte signatures for the formats we accept. Checked against the first
# bytes of the uploaded file; extension alone is not trusted.
MAGIC_SIGNATURES = {
    ".mp3":  [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    ".flac": [b"fLaC"],
    ".wav":  [b"RIFF"],
    ".ogg":  [b"OggS"],
    ".opus": [b"OggS"],
    # m4a/aac use an ftyp box a few bytes in, or raw ADTS sync word
    ".m4a":  [b"ftyp", b"\xff\xf1", b"\xff\xf9"],
    ".aac":  [b"ftyp", b"\xff\xf1", b"\xff\xf9"],
}

# ====================================
# LOGGING
# ====================================

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("alpha")

# ====================================
# FLASK APP
# ====================================

class _BoundedRequest(_FlaskRequest):
    max_content_length   = MAX_UPLOAD_BYTES
    max_form_parts       = 1000
    max_form_memory_size = 2 * 1024 * 1024  # small — audio goes to disk, not memory

app = Flask(__name__)
app.request_class = _BoundedRequest
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

OUTPUT_FOLDER = tempfile.mkdtemp(prefix="alpha_")
log.info("Output folder: %s (max upload %sMB, cleanup TTL %ss)",
          OUTPUT_FOLDER, MAX_UPLOAD_MB, CLEANUP_TTL_SECONDS)


# ====================================
# FIX EP TITLE FROM FILENAME
# ====================================

def fix_ep_title(name):
    name = name.strip()

    if '_' in name and ' ' not in name:
        name = name.replace('_', ' ')

    pattern = re.compile(r'^(Ep(?:i(?:sode)?)?)[\s._\-]+(\d+)(.*)$', re.IGNORECASE)
    match = pattern.match(name)
    if match:
        return f"Ep {match.group(2)}{match.group(3)}"

    pattern2 = re.compile(r'^(Ep(?:i(?:sode)?)?)(\d+)(.*)$', re.IGNORECASE)
    match2 = pattern2.match(name)
    if match2:
        return f"Ep {match2.group(2)}{match2.group(3)}"

    return name


def fix_ep_filename(original_name):
    base, ext    = os.path.splitext(original_name)
    new_title    = fix_ep_title(base)
    new_filename = new_title + ext
    return new_filename, new_title


# ====================================
# SAFE FILENAME / PATH-TRAVERSAL GUARDS
# ====================================

def safe_internal_name(name):
    """Windows-forbidden chars stripped; used for display/tagging."""
    return re.sub(r'[\\/:*?"<>|]', '_', name)


def safe_disk_name(name):
    """
    Strict name for anything that touches the filesystem. secure_filename()
    strips path separators, drive letters, and leading dots, which is what
    stops '../../etc/passwd'-style traversal. Falls back to a uuid if the
    name sanitizes to empty (e.g. an all-unicode filename).
    """
    cleaned = secure_filename(name)
    if not cleaned:
        cleaned = f"file_{uuid.uuid4().hex[:8]}"
    return cleaned


def resolve_within(base_dir, *parts):
    """
    Join parts under base_dir and verify the result doesn't escape it.
    Raises ValueError on any traversal attempt. Use for every path built
    from user-supplied input (download route especially).
    """
    base_dir = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base_dir, *parts))
    if not (candidate == base_dir or candidate.startswith(base_dir + os.sep)):
        raise ValueError("Path traversal attempt blocked")
    return candidate


# ====================================
# FILE TYPE VALIDATION (extension + magic bytes)
# ====================================

def validate_audio_file(filename, stream):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file extension: '{ext}'")

    stream.seek(0)
    header = stream.read(64)
    stream.seek(0)

    if not header:
        raise ValueError("Empty file")

    signatures = MAGIC_SIGNATURES.get(ext, [])
    # ftyp box for m4a/aac isn't at offset 0 — it's at offset 4
    matched = any(
        header.startswith(sig) or (sig == b"ftyp" and header[4:8] == b"ftyp")
        for sig in signatures
    )
    if not matched:
        raise ValueError(
            f"File content doesn't match a valid '{ext}' file "
            f"(magic-byte check failed) — possible spoofed extension"
        )
    return ext


# ====================================
# TTL-BASED CLEANUP
# ====================================
#
# NOTE: cleanup can't happen in a try/finally around the upload request —
# the output file needs to stay on disk until the client hits /download/...,
# which is a *separate* request. So this runs as a background sweep that
# deletes work-dirs older than CLEANUP_TTL_SECONDS. If your TTL is shorter
# than how long users take to click "download" after upload, downloads
# will start failing — tune CLEANUP_TTL_SECONDS accordingly.

def _cleanup_loop():
    while True:
        try:
            now = time.time()
            for entry in os.scandir(OUTPUT_FOLDER):
                if not entry.is_dir():
                    continue
                try:
                    age = now - entry.stat().st_mtime
                    if age > CLEANUP_TTL_SECONDS:
                        shutil.rmtree(entry.path, ignore_errors=True)
                        log.info("Cleaned up expired workspace: %s (age %.0fs)",
                                  entry.name, age)
                except FileNotFoundError:
                    pass
        except Exception:
            log.exception("Cleanup sweep failed")
        time.sleep(CLEANUP_INTERVAL_SECONDS)


threading.Thread(target=_cleanup_loop, daemon=True).start()


# ====================================
# SHARED: PROCESS ONE AUDIO FILE
# ====================================

def process_one(audio_file):
    original_name = audio_file.filename
    if not original_name:
        raise ValueError("No filename provided")

    # Validate BEFORE writing anything to disk
    ext = validate_audio_file(original_name, audio_file.stream)

    new_filename, new_title = fix_ep_filename(original_name)

    safe_orig = safe_disk_name(original_name)
    safe_new  = safe_disk_name(new_filename)

    req_id   = uuid.uuid4().hex[:12]
    work_dir = resolve_within(OUTPUT_FOLDER, req_id)
    os.makedirs(work_dir, exist_ok=True)

    raw_path    = resolve_within(work_dir, "raw_" + safe_orig)
    output_path = resolve_within(work_dir, safe_new)

    try:
        audio_file.stream.seek(0, 2)
        stream_size = audio_file.stream.tell()
        audio_file.stream.seek(0)
        if stream_size == 0:
            raise ValueError(
                "Upload stream is empty (0 bytes) — check client-side upload "
                "and MAX_CONTENT_LENGTH"
            )

        audio_file.save(raw_path)

        if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            raise RuntimeError(f"File did not save to disk: {raw_path}")

        shutil.copy2(raw_path, output_path)

        audio = File(output_path, easy=False)

        if audio is None and ext in (".m4a", ".aac"):
            remuxed = resolve_within(work_dir, "remuxed_" + safe_orig)
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", output_path,
                     "-c", "copy", "-movflags", "+faststart", remuxed],
                    capture_output=True, timeout=60, check=True
                )
                shutil.copy2(remuxed, output_path)
                audio = File(output_path, easy=False)
            except Exception:
                log.warning("ffmpeg remux failed for %s", safe_orig, exc_info=True)

        if audio is None:
            size   = os.path.getsize(output_path)
            header = open(output_path, "rb").read(12).hex()
            raise RuntimeError(
                f"Cannot parse '{ext}' — size:{size}B header:{header}. "
                f"Install ffmpeg or convert to mp3."
            )

        if ext == ".mp3":
            try:
                audio.add_tags()
            except Exception:
                pass
            audio["TPE1"] = TPE1(encoding=3, text=ARTIST_NAME)
            audio["TALB"] = TALB(encoding=3, text=ALBUM_NAME)
            audio["TIT2"] = TIT2(encoding=3, text=new_title)
            with open(COVER_IMAGE, "rb") as img:
                audio.tags.add(APIC(encoding=3, mime="image/jpeg",
                                    type=3, desc="Cover", data=img.read()))

        elif ext == ".flac":
            from mutagen.flac import Picture
            audio["artist"] = ARTIST_NAME
            audio["album"]  = ALBUM_NAME
            audio["title"]  = new_title
            pic = Picture()
            with open(COVER_IMAGE, "rb") as img:
                pic.data = img.read()
            pic.type = 3
            pic.mime = "image/jpeg"
            audio.clear_pictures()
            audio.add_picture(pic)

        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4Cover
            audio["\xa9ART"] = [ARTIST_NAME]
            audio["\xa9alb"] = [ALBUM_NAME]
            audio["\xa9nam"] = [new_title]
            with open(COVER_IMAGE, "rb") as img:
                audio["covr"] = [MP4Cover(img.read(), imageformat=MP4Cover.FORMAT_JPEG)]

        elif ext in (".ogg", ".opus"):
            import base64
            audio["artist"] = ARTIST_NAME
            audio["album"]  = ALBUM_NAME
            audio["title"]  = new_title
            with open(COVER_IMAGE, "rb") as img:
                audio["metadata_block_picture"] = [
                    base64.b64encode(img.read()).decode("ascii")
                ]

        else:  # .wav
            try:
                audio["artist"] = ARTIST_NAME
                audio["album"]  = ALBUM_NAME
                audio["title"]  = new_title
            except Exception:
                pass

        audio.save()
        final_size = os.path.getsize(output_path)

        return {
            "name": new_filename,
            "url":  f"/download/{req_id}/{safe_new}",
            "size_bytes": final_size,
        }

    except Exception:
        # Processing failed — this request's workspace is useless, remove it
        # now rather than waiting for the TTL sweep. Successful runs are left
        # alone (the download route still needs them).
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


# ====================================
# ROUTES
# ====================================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/upload_one", methods=["POST"])
def upload_one():
    try:
        audio_file = request.files.get("audio")
        if not audio_file:
            return jsonify({"error": "No file received"}), 400
        result = process_one(audio_file)
        return jsonify(result), 200
    except ValueError as e:
        log.info("Rejected upload: %s", e)
        return jsonify({"error": str(e)}), 400
    except Exception:
        log.exception("upload_one failed")
        return jsonify({"error": "Internal processing error"}), 500


@app.route("/upload", methods=["POST"])
def upload():
    try:
        audio_files = request.files.getlist("audio")
        if not audio_files:
            return jsonify([]), 400
        results = []
        for f in audio_files:
            try:
                results.append(process_one(f))
            except ValueError as e:
                results.append({"error": str(e), "name": f.filename})
            except Exception:
                log.exception("upload (batch) failed for %s", f.filename)
                results.append({"error": "Internal processing error", "name": f.filename})
        return jsonify(results), 200
    except Exception:
        log.exception("upload failed")
        return jsonify({"error": "Internal processing error"}), 500


@app.route("/download/<req_id>/<filename>")
def download(req_id, filename):
    try:
        # req_id must be exactly the uuid hex segment we generated — reject
        # anything else before it ever touches the filesystem.
        if not re.fullmatch(r"[0-9a-f]{12}", req_id):
            abort(404)
        file_path = resolve_within(OUTPUT_FOLDER, req_id, secure_filename(filename))
    except ValueError:
        abort(404)

    if not os.path.isfile(file_path):
        abort(404)

    return send_file(file_path, as_attachment=True,
                     download_name=os.path.basename(file_path))


@app.route("/check")
def check():
    return jsonify({
        "ffmpeg":        shutil.which("ffmpeg")  or "NOT FOUND",
        "ffprobe":       shutil.which("ffprobe") or "NOT FOUND",
        "output_folder": OUTPUT_FOLDER,
        "cover_exists":  os.path.exists(COVER_IMAGE),
        "max_upload_mb": MAX_UPLOAD_MB,
    })


# ====================================
# RUN SERVER
# ====================================

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port  = int(os.environ.get("PORT", "5000"))
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
