
import os
import re
import shutil
import tempfile
from pathlib import Path

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

APP_VERSION = "v9-merged-mp4"
BASE_DIR = Path(__file__).resolve().parent

# cookies.txt GitHub-এ আপলোড করবে না।
# প্রয়োজন হলে environment variable দিয়ে private file path দেবে।
COOKIE_PATH = Path(
    os.environ.get("YOUTUBE_COOKIES_PATH", BASE_DIR / "cookies.txt")
)

MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", "500"))

ATTEMPTS = [
    {"clients": ["web_safari"], "cookies": True},
    {"clients": ["android_vr"], "cookies": False},
    {"clients": ["tv_simply"], "cookies": False},
    {"clients": ["ios"], "cookies": False},
]


class CaptureLogger:
    KEYWORDS = (
        "javascript", "runtime", "quickjs", "qjs",
        "ejs", "challenge", "deno", "node"
    )

    def __init__(self):
        self.messages = []
        self.debug_msgs = []

    def debug(self, msg):
        value = str(msg)
        if any(k in value.lower() for k in self.KEYWORDS):
            if len(self.debug_msgs) < 30:
                self.debug_msgs.append(value[:300])

    def info(self, msg):
        pass

    def warning(self, msg):
        self.messages.append(str(msg)[:500])

    def error(self, msg):
        self.messages.append(str(msg)[:500])


def is_valid_youtube_url(url):
    if not isinstance(url, str):
        return False

    pattern = (
        r"^(https?://)?(www\.)?"
        r"(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)"
        r"[\w-]+(\S*)?$"
    )

    return re.match(pattern, url.strip()) is not None


def request_url():
    data = request.get_json(silent=True) or {}
    url = data.get("url")

    if not is_valid_youtube_url(url):
        return None

    return url.strip()


def get_ydl_opts(extra_opts=None, clients=None, use_cookies=True):

    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignore_no_formats_error": True,
        "check_formats": False,
        "logger": CaptureLogger(),
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
    }

    if clients:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": clients
            }
        }

    if use_cookies and COOKIE_PATH.is_file():
        opts["cookiefile"] = str(COOKIE_PATH)

    if extra_opts:
        opts.update(extra_opts)

    return opts


def real_formats(info):
    return [
        f for f in (info.get("formats") or [])
        if f.get("vcodec") != "none"
        or f.get("acodec") != "none"
    ]


def extract_with_fallback(url, extra_opts=None):

    body = request.get_json(silent=True) or {}

    if body.get("clients"):
        attempts = [{
            "clients": body["clients"],
            "cookies": bool(body.get("use_cookies", False))
        }]
    else:
        attempts = ATTEMPTS

    logs = []

    for attempt in attempts:

        opts = get_ydl_opts(
            extra_opts,
            attempt["clients"],
            attempt["cookies"]
        )

        logger = opts["logger"]

        entry = {
            "clients": attempt["clients"],
            "cookies": attempt["cookies"]
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            formats = real_formats(info)

            entry["real_formats"] = len(formats)
            entry["messages"] = logger.messages
            entry["js_debug"] = logger.debug_msgs

            logs.append(entry)

            if formats:
                return info, formats, logs

        except Exception as exc:

            entry["error"] = str(exc)[:700]
            entry["messages"] = logger.messages

            logs.append(entry)

    return None, [], logs


def resolution_height(resolution):

    if resolution == "highest":
        return None

    value = str(resolution).lower().removesuffix("p")

    if not value.isdigit():
        raise ValueError("Invalid resolution.")

    height = int(value)

    if not 144 <= height <= 4320:
        raise ValueError("Resolution must be between 144p and 4320p.")

    return height


def format_selector(height):

    if height is None:
        return (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/best"
        )

    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]"
    )


def _file_info(f):

    return {
        "url": f.get("url"),
        "height": f.get("height"),
        "ext": f.get("ext"),
        "vcodec": f.get("vcodec"),
        "acodec": f.get("acodec"),
        "bitrate_kbps": f.get("tbr") or f.get("abr"),
        "filesize": f.get("filesize") or f.get("filesize_approx"),
        "format_id": f.get("format_id"),
    }


@app.get("/")
def home():

    return jsonify({
        "status": "API is running successfully!",
        "app_version": APP_VERSION
    })


@app.get("/health")
def health():

    return jsonify({
        "status": "healthy"
    }), 200


@app.get("/debug")
def debug():

    import importlib.util

    return jsonify({
        "app_version": APP_VERSION,
        "yt_dlp_version": yt_dlp.version.__version__,
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "node": shutil.which("node"),
        "deno": shutil.which("deno"),
        "yt_dlp_ejs_installed":
            importlib.util.find_spec("yt_dlp_ejs") is not None,
        "cookies_exists": COOKIE_PATH.is_file(),
        "cookies_size":
            COOKIE_PATH.stat().st_size if COOKIE_PATH.is_file() else None,
    })


@app.post("/video_info")
def video_info():

    url = request_url()

    if not url:
        return jsonify({
            "error": "Invalid or missing YouTube URL."
        }), 400

    try:

        info, formats, logs = extract_with_fallback(url)

        if not info:
            return jsonify({
                "error": "Could not retrieve video information.",
                "attempts": logs
            }), 502

        return jsonify({
            "title": info.get("title"),
            "author": info.get("uploader") or info.get("channel"),
            "length": info.get("duration"),
            "views": info.get("view_count"),
            "thumbnail": info.get("thumbnail"),
            "description": info.get("description"),
            "publish_date": info.get("upload_date"),
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/available_resolutions")
def available_resolutions():

    url = request_url()

    if not url:
        return jsonify({
            "error": "Invalid or missing YouTube URL."
        }), 400

    try:

        info, formats, logs = extract_with_fallback(url)

        if not formats:
            return jsonify({
                "error": "No video formats were returned.",
                "attempts": logs,
                "progressive": [],
                "all": []
            }), 502

        all_res = set()
        progressive_res = set()
        details = []

        for f in formats:

            if f.get("vcodec") != "none" and f.get("height"):

                label = f"{f['height']}p"
                all_res.add(label)

                has_audio = f.get("acodec") != "none"

                if has_audio:
                    progressive_res.add(label)

                details.append({
                    "height": f.get("height"),
                    "ext": f.get("ext"),
                    "protocol": f.get("protocol"),
                    "video": True,
                    "audio": has_audio
                })

        sort_key = lambda x: int(x[:-1])

        return jsonify({
            "progressive": sorted(
                progressive_res,
                key=sort_key
            ),
            "all": sorted(
                all_res,
                key=sort_key
            ),
            "worked_with": logs[-1]["clients"],
            "details": details
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/download/<resolution>")
def download_link(resolution):

    url = request_url()

    if not url:
        return jsonify({
            "error": "Invalid or missing YouTube URL."
        }), 400

    try:
        height = resolution_height(resolution)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    selector = format_selector(height)

    try:

        info, formats, logs = extract_with_fallback(
            url,
            {"format": selector}
        )

        if not info:
            return jsonify({
                "error": "Could not select a downloadable format.",
                "attempts": logs
            }), 502

        direct_url = info.get("url")

        if not direct_url:

            requested = info.get("requested_formats") or []

            if requested:
                direct_url = requested[0].get("url")

        if not direct_url:
            return jsonify({
                "error": "Selected format may contain separate streams. Use the merged MP4 endpoint.",
                "title": info.get("title")
            }), 409

        return jsonify({
            "message": "Download link generated successfully.",
            "title": info.get("title"),
            "download_url": direct_url,
            "worked_with": logs[-1]["clients"],
            "selected": {
                "height": info.get("height"),
                "ext": info.get("ext"),
                "protocol": info.get("protocol"),
                "video": info.get("vcodec") != "none",
                "audio": info.get("acodec") != "none",
                "format_id": info.get("format_id")
            }
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/download_file/<resolution>")
def download_merged_mp4(resolution):

    url = request_url()

    if not url:
        return jsonify({
            "error": "Invalid or missing YouTube URL."
        }), 400

    if not shutil.which("ffmpeg"):
        return jsonify({
            "error": "FFmpeg is not installed on the server."
        }), 503

    try:
        height = resolution_height(resolution)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    temp_dir = Path(tempfile.mkdtemp(prefix="ytdlp_"))
    selector = format_selector(height)
    logs = []

    try:

        downloaded_info = None

        for attempt in ATTEMPTS:

            logger = CaptureLogger()

            opts = get_ydl_opts({

                "format": selector,

                "outtmpl": str(
                    temp_dir / "%(title).120B [%(id)s].%(ext)s"
                ),

                "merge_output_format": "mp4",
                "noplaylist": True,
                "continuedl": False,
                "overwrites": True,

                "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,

                "quiet": True,
                "no_warnings": True,
                "logger": logger

            }, attempt["clients"], attempt["cookies"])

            entry = {
                "clients": attempt["clients"],
                "cookies": attempt["cookies"]
            }

            try:

                with yt_dlp.YoutubeDL(opts) as ydl:
                    downloaded_info = ydl.extract_info(
                        url,
                        download=True
                    )

                entry["messages"] = logger.messages
                logs.append(entry)
                break

            except Exception as exc:

                entry["error"] = str(exc)[:700]
                entry["messages"] = logger.messages
                logs.append(entry)
                downloaded_info = None

        candidates = [
            p for p in temp_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".mp4"
        ]

        if not candidates:
            candidates = [
                p for p in temp_dir.iterdir()
                if p.is_file()
                and p.suffix.lower() not in (".part", ".ytdl")
            ]

        if not candidates:
            return jsonify({
                "error": "Download or merge failed.",
                "attempts": logs
            }), 502

        output = max(
            candidates,
            key=lambda p: p.stat().st_size
        )

        if output.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
            return jsonify({
                "error": f"File exceeds {MAX_DOWNLOAD_MB} MB limit."
            }), 413

        title = (
            (downloaded_info or {}).get("title")
            or output.stem
        )

        safe_title = re.sub(
            r'[\\/:*?"<>|]+',
            "_",
            title
        ).strip(" .")[:120] or "video"

        filename = (
            f"{safe_title}.mp4"
            if output.suffix.lower() == ".mp4"
            else output.name
        )

        response = send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype=(
                "video/mp4"
                if output.suffix.lower() == ".mp4"
                else "application/octet-stream"
            ),
            conditional=True
        )

        response.call_on_close(
            lambda: shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )
        )

        return response

    except Exception as exc:

        shutil.rmtree(temp_dir, ignore_errors=True)

        return jsonify({
            "error": str(exc)[:1000],
            "attempts": logs
        }), 500


@app.post("/streams/<resolution>")
def streams(resolution):

    url = request_url()

    if not url:
        return jsonify({
            "error": "Invalid or missing YouTube URL."
        }), 400

    try:

        target = resolution_height(resolution)

        info, formats, logs = extract_with_fallback(url)

        if not formats:
            return jsonify({
                "error": "No formats from any client.",
                "attempts": logs
            }), 502

        direct = [
            f for f in formats
            if f.get("protocol") == "https"
            and f.get("url")
        ]

        def within(f):
            return (
                target is None
                or (f.get("height") or 0) <= target
            )

        def vkey(f):
            return (
                f.get("height") or 0,
                f.get("ext") == "mp4",
                f.get("tbr") or 0
            )

        video_only = [
            f for f in direct
            if f.get("vcodec") != "none"
            and f.get("acodec") == "none"
            and f.get("height")
            and within(f)
        ]

        progressive = [
            f for f in direct
            if f.get("vcodec") != "none"
            and f.get("acodec") != "none"
            and f.get("height")
            and within(f)
        ]

        audio_only = [
            f for f in direct
            if f.get("acodec") != "none"
            and f.get("vcodec") == "none"
        ]

        best_video = (
            max(video_only, key=vkey)
            if video_only else None
        )

        best_prog = (
            max(progressive, key=vkey)
            if progressive else None
        )

        best_audio = (
            max(
                audio_only,
                key=lambda f: (
                    f.get("ext") == "m4a",
                    f.get("abr") or 0
                )
            )
            if audio_only else None
        )

        if not best_video and not best_prog:
            return jsonify({
                "error": "No direct video format found.",
                "attempts": logs
            }), 404

        merge_required = bool(
            best_video
            and best_audio
            and (
                not best_prog
                or (best_video.get("height") or 0)
                > (best_prog.get("height") or 0)
            )
        )

        return jsonify({
            "title": info.get("title"),
            "requested": resolution,
            "merge_required": merge_required,
            "video": (
                _file_info(best_video)
                if best_video else None
            ),
            "audio": (
                _file_info(best_audio)
                if best_audio else None
            ),
            "progressive": (
                _file_info(best_prog)
                if best_prog else None
            ),
            "worked_with": logs[-1]["clients"]
        })

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":

    from waitress import serve

    serve(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080"))
    )
