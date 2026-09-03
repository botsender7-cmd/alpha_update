
from flask import Flask, render_template, request, jsonify, send_file
from flask.wrappers import Request as _FlaskRequest

from mutagen import File
from mutagen.id3 import APIC, TPE1, TALB, TIT2

import os
import re
import tempfile
import subprocess
import uuid

app = Flask(__name__)

# Must subclass Flask's Request (not Werkzeug's) — Flask adds blueprints etc on top
class _UnlimitedRequest(_FlaskRequest):
    max_content_length   = None
    max_form_parts       = 100000
    max_form_memory_size = 500 * 1024 * 1024  # 500 MB
app.request_class = _UnlimitedRequest
app.config["MAX_CONTENT_LENGTH"] = None

# ====================================
# YOUR SAVED DATA
# ====================================

# If ffmpeg is not on your system PATH, set its full path here manually.
# Example (Windows):  r"C:\ffmpeg\bin\ffmpeg.exe"
# Example (Mac/Linux): "/usr/local/bin/ffmpeg"
# Leave as "ffmpeg" only if `ffmpeg -version` already works in your terminal as-is.
FFMPEG_PATH = "ffmpeg"

ARTIST_NAME = "@king75683"

ALBUM_NAME = "@king75683"

COVER_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image_k.jpg")

# ====================================
# TEMP OUTPUT FOLDER
# ====================================

OUTPUT_FOLDER = tempfile.mkdtemp()

# ====================================
# FIX EP TITLE FROM FILENAME
# ====================================

def fix_ep_title(name):
    name = name.strip()

    # If fully underscore-separated (no spaces at all), replace all _ with space
    if '_' in name and ' ' not in name:
        name = name.replace('_', ' ')

    # Normalize separator between Ep/Epi/Episode and the number
    # Also normalizes "Epi" → "Ep"
    pattern = re.compile(r'^(Ep(?:i(?:sode)?)?)[\s._\-]+(\d+)(.*)$', re.IGNORECASE)
    match = pattern.match(name)
    if match:
        ep_num = match.group(2)
        rest   = match.group(3)
        return f"Ep {ep_num}{rest}"

    # No separator at all: Ep2012... / Epi2012...
    pattern2 = re.compile(r'^(Ep(?:i(?:sode)?)?)(\d+)(.*)$', re.IGNORECASE)
    match2 = pattern2.match(name)
    if match2:
        return f"Ep {match2.group(2)}{match2.group(3)}"

    return name


def fix_ep_filename(original_name):
    base, ext = os.path.splitext(original_name)
    # Extension is decided later, from the REAL detected format of the file
    # content — not from this original extension, and not forced to .mp3.
    return fix_ep_title(base)


# ====================================
# DETECT REAL AUDIO FORMAT (from content, not filename extension)
# ====================================
# We never trust the uploaded filename's extension — a file can be named
# "song.mp3" while actually containing m4a/aac/etc data. ffmpeg's own probe
# of the file (via `-i`, reading its stderr stream info) tells us what the
# content actually is, and we output in THAT format.

FORMAT_MAP = {
    "mp3":  {"ext": ".mp3",  "args": ["-vn", "-codec:a", "libmp3lame", "-q:a", "2"],  "tag": "id3"},
    "wav":  {"ext": ".wav",  "args": ["-vn", "-codec:a", "pcm_s16le"],                "tag": "id3"},
    "m4a":  {"ext": ".m4a",  "args": ["-vn", "-codec:a", "aac", "-b:a", "192k"],       "tag": "mp4"},
    "flac": {"ext": ".flac", "args": ["-vn", "-codec:a", "flac"],                     "tag": "flac"},
    "ogg":  {"ext": ".ogg",  "args": ["-vn", "-codec:a", "libvorbis", "-q:a", "5"],    "tag": "ogg"},
}

def detect_real_format(path):
    """Returns one of the FORMAT_MAP keys, or None if undetected/unsupported."""
    try:
        proc = subprocess.run([FFMPEG_PATH, "-i", path], capture_output=True, timeout=60)
    except FileNotFoundError:
        raise Exception(
            f"ffmpeg not found at '{FFMPEG_PATH}'. Edit FFMPEG_PATH near the top of "
            "this file and set it to ffmpeg's full path on your system."
        )
    stderr = proc.stderr.decode(errors="ignore")
    m = re.search(r"Audio:\s*([a-zA-Z0-9_]+)", stderr)
    if not m:
        return None
    codec = m.group(1).lower()
    if "mp3" in codec:
        return "mp3"
    if "aac" in codec or "alac" in codec:
        return "m4a"
    if "pcm" in codec:
        return "wav"
    if "flac" in codec:
        return "flac"
    if "vorbis" in codec:
        return "ogg"
    return None


# ====================================
# FORMAT-SPECIFIC TAGGING
# (each container has a different tagging scheme — ID3 only applies to mp3/wav)
# ====================================

def tag_id3(output_path, title, artist, album, cover_bytes):
    audio = File(output_path, easy=False)
    if audio is None:
        size   = os.path.getsize(output_path)
        header = open(output_path, "rb").read(12).hex()
        raise Exception(f"Converted file could not be parsed by mutagen — size:{size}B header:{header}.")
    try:
        audio.add_tags()
    except Exception:
        pass
    if audio.tags is None or not hasattr(audio.tags, "add"):
        raise Exception(
            f"Encoded output at '{output_path}' does not carry ID3 tags "
            f"(got tag type: {type(audio.tags).__name__})."
        )
    audio["TPE1"] = TPE1(encoding=3, text=artist)
    audio["TALB"] = TALB(encoding=3, text=album)
    audio["TIT2"] = TIT2(encoding=3, text=title)
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
    # v2_version=3 (ID3v2.3): many real-world players (Windows Explorer
    # thumbnails, older car stereos, some phone apps) don't render cover art
    # from ID3v2.4 even though the tag is technically written correctly.
    audio.save(v2_version=3)


def tag_mp4(output_path, title, artist, album, cover_bytes):
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(output_path)
    audio["\xa9nam"] = [title]
    audio["\xa9ART"] = [artist]
    audio["\xa9alb"] = [album]
    audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def tag_flac(output_path, title, artist, album, cover_bytes):
    from mutagen.flac import FLAC, Picture
    audio = FLAC(output_path)
    audio["title"]  = [title]
    audio["artist"] = [artist]
    audio["album"]  = [album]
    audio.clear_pictures()
    pic = Picture()
    pic.data = cover_bytes
    pic.type = 3
    pic.mime = "image/jpeg"
    audio.add_picture(pic)
    audio.save()


def tag_ogg(output_path, title, artist, album, cover_bytes):
    import base64
    from mutagen.oggvorbis import OggVorbis
    from mutagen.flac import Picture
    audio = OggVorbis(output_path)
    audio["title"]  = [title]
    audio["artist"] = [artist]
    audio["album"]  = [album]
    pic = Picture()
    pic.data = cover_bytes
    pic.type = 3
    pic.mime = "image/jpeg"
    audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()


TAGGERS = {"id3": tag_id3, "mp4": tag_mp4, "flac": tag_flac, "ogg": tag_ogg}


# ====================================
# SAFE INTERNAL FILENAME
# Only strip actual Windows-forbidden chars: \ / : * ? " < > |
# Spaces, !, Hindi chars are all fine on Windows NTFS
# ====================================

def safe_internal_name(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name)


# ====================================
# SHARED: PROCESS ONE AUDIO FILE
# ====================================

def process_one(audio_file):
    original_name = audio_file.filename
    new_title     = fix_ep_filename(original_name)

    # Use safe name for the raw upload path (final output name depends on the
    # REAL detected format, decided further below once raw_path exists on disk)
    safe_orig = safe_internal_name(original_name)

    # Unique subfolder per request. Filenames alone are NOT a safe key for
    # temp paths — two requests for the same original filename (double
    # submit, a retry racing the original attempt, etc) would otherwise
    # share raw_path/resized_tmp and one request's write/cleanup can delete
    # a file the other is mid-read on ("No such file or directory").
    req_id   = uuid.uuid4().hex[:12]
    work_dir = os.path.join(OUTPUT_FOLDER, req_id)
    os.makedirs(work_dir, exist_ok=True)

    raw_path = os.path.join(work_dir, "raw_" + safe_orig)

    # Check stream has data before saving
    audio_file.stream.seek(0, 2)
    stream_size = audio_file.stream.tell()
    audio_file.stream.seek(0)
    if stream_size == 0:
        raise Exception(
            "Upload stream is empty (0 bytes). "
            "Flask/Werkzeug hit a form limit. Check MAX_CONTENT_LENGTH and MAX_FORM_PARTS."
        )

    audio_file.save(raw_path)

    if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        raise Exception(f"File did not save to disk. Path: {raw_path}")

    # ====================================
    # DETECT REAL FORMAT, THEN CONVERT/RE-ENCODE TO THAT SAME FORMAT
    # ====================================
    detected = detect_real_format(raw_path)
    if detected is None:
        # Genuinely couldn't identify the codec (corrupt file, or a format
        # outside mp3/wav/m4a/flac/ogg) — fail loudly rather than silently
        # guessing a format, since that's exactly the class of bug we're avoiding.
        raise Exception(
            f"Could not detect a supported audio format in the uploaded file "
            f"(original name: '{original_name}'). Supported: mp3, wav, m4a/aac, flac, ogg."
        )
    fmt = FORMAT_MAP[detected]

    new_filename = new_title + fmt["ext"]
    safe_new     = safe_internal_name(new_filename)
    output_path  = os.path.join(work_dir, safe_new)

    try:
        proc = subprocess.run(
            [FFMPEG_PATH, "-y", "-i", raw_path, *fmt["args"], output_path],
            capture_output=True, timeout=300
        )
        if proc.returncode != 0 or not os.path.exists(output_path):
            raise Exception(
                f"ffmpeg conversion failed for detected format '{detected}': "
                f"{proc.stderr.decode(errors='ignore')[-500:]}"
            )
    except FileNotFoundError:
        raise Exception(
            f"ffmpeg not found at '{FFMPEG_PATH}'. Edit FFMPEG_PATH near the top of "
            "this file and set it to ffmpeg's full path on your system."
        )
    except PermissionError as e:
        # Windows raises this as [WinError 5] Access is denied when the path
        # resolves but can't be executed. Common causes, in order of likelihood:
        #  1. FFMPEG_PATH points at a folder, not the .exe itself
        #  2. Windows has the exe "blocked" (right-click -> Properties -> Unblock)
        #  3. Antivirus/Defender is quarantining or blocking execution.
        #  4. ffmpeg sits in an admin-protected folder and this app isn't elevated.
        is_dir = os.path.isdir(FFMPEG_PATH)
        raise Exception(
            f"Access denied trying to run ffmpeg at '{FFMPEG_PATH}'"
            + (" — this path is a FOLDER, it must point to the ffmpeg.exe file itself."
               if is_dir else ".")
            + " If the path is correct: right-click ffmpeg.exe -> Properties -> "
              "check 'Unblock' on the General tab -> Apply. Otherwise check antivirus "
              f"and folder permissions. Original error: {e}"
        )

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise Exception(f"Output file was not created. Path: {output_path}")

    # ---- Read cover image once ----
    try:
        with open(COVER_IMAGE, "rb") as img:
            cover_bytes = img.read()
    except PermissionError as e:
        raise Exception(
            f"Access denied reading cover image at '{COVER_IMAGE}'. "
            f"Check the file exists and isn't blocked/locked by another program. Original error: {e}"
        )
    except FileNotFoundError:
        raise Exception(
            f"Cover image not found at '{COVER_IMAGE}'. Make sure image_k.jpg "
            "sits next to this script."
        )

    # ---- Tag using the scheme appropriate for this format ----
    try:
        TAGGERS[fmt["tag"]](output_path, new_title, ARTIST_NAME, ALBUM_NAME, cover_bytes)
    except PermissionError as e:
        raise Exception(
            f"Access denied writing to '{output_path}'. Check that OUTPUT_FOLDER "
            f"isn't a protected system directory and this app has write permission there. Original error: {e}"
        )

    final_size = os.path.getsize(output_path)

    result = {
        "name": new_filename,            # pretty name shown in UI
        "url":  f"/download/{req_id}/{safe_new}", # safe path for download route
        "size_bytes": final_size,
    }
    return result


# ====================================
# HOME
# ====================================

@app.route("/")
def home():
    return render_template("index.html")


# ====================================
# UPLOAD ONE FILE
# ====================================

@app.route("/upload_one", methods=["POST"])
def upload_one():
    try:
        audio_file = request.files.get("audio")
        if not audio_file:
            return jsonify({"error": "No file received"})
        result = process_one(audio_file)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})


# ====================================
# UPLOAD MULTIPLE (legacy)
# ====================================

@app.route("/upload", methods=["POST"])
def upload():
    try:
        audio_files = request.files.getlist("audio")
        if not audio_files:
            return jsonify([])
        results = [process_one(f) for f in audio_files]
        return jsonify(results)
    except Exception as e:
        return str(e)


# ====================================
# DOWNLOAD
# ====================================

@app.route("/download/<path:filename>")
def download(filename):
    file_path = os.path.join(OUTPUT_FOLDER, filename)
    return send_file(file_path, as_attachment=True,
                     download_name=os.path.basename(filename))


# ====================================
# DEBUG: CHECK ENVIRONMENT
# ====================================

@app.route("/check")
def check():
    import shutil as sh
    return jsonify({
        "ffmpeg":        sh.which("ffmpeg")  or "NOT FOUND",
        "ffprobe":       sh.which("ffprobe") or "NOT FOUND",
        "output_folder": OUTPUT_FOLDER,
        "cover_exists":  os.path.exists(COVER_IMAGE),
        "werkzeug":      __import__("importlib.metadata").metadata.version("werkzeug"),
    })


# ====================================
# RUN SERVER
# ====================================

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
