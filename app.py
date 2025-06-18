from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
import os
import yt_dlp
import re
import threading
import time
import subprocess
import logging

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_DURATION_SECONDS = 150 * 60  # 2.5 hours
MAX_FILENAME_LENGTH = 128

# Rate limiting per user_id
def get_user_id():
    if request.method == "POST" and request.is_json:
        return str(request.get_json().get("user_id", "anonymous"))
    return "anonymous"

limiter = Limiter(
    key_func=get_user_id,
    app=app,
    default_limits=["30 per hour"]
)

def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        logger.error("FFmpeg is missing or not in PATH.")
        raise EnvironmentError("FFmpeg is not installed or not in system PATH.")
check_ffmpeg()

def sanitize_filename(title):
    clean = re.sub(r'[\\/*?:"<>|]', '_', title)
    return clean[:MAX_FILENAME_LENGTH]

def delete_file_later(filepath, delay=600):
    def delete():
        time.sleep(delay)
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Deleted: {filepath}")
    threading.Thread(target=delete, daemon=True).start()

@app.route('/')
def index():
    return 'YT-MP3 API is live 🎶'

@app.route("/convert", methods=["POST"])
@limiter.limit("3 per minute")
def convert():
    if not request.is_json:
        return jsonify({"error": "Invalid request format. JSON expected."}), 400

    data = request.get_json()
    url = data.get("url")
    file_format = data.get("format", "mp3").lower()
    user_id = data.get("user_id")

    if not url or not re.match(r'^https?://(www\.)?(youtube\.com|youtu\.be)/', url):
        return jsonify({"error": "Only valid YouTube URLs are supported."}), 400
    if not user_id:
        return jsonify({"error": "Missing user_id in request."}), 400
    if file_format not in ["mp3", "mp4"]:
        return jsonify({"error": "Invalid format. Use 'mp3' or 'mp4'."}), 400

    try:
        # Use cookies.txt for YouTube session authentication
        with yt_dlp.YoutubeDL({'quiet': True, 'cookiefile': 'cookies.txt'}) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get("duration", 0)
            if duration > MAX_DURATION_SECONDS:
                return jsonify({"error": "Video exceeds max duration (2.5 hours)."}), 400

            title = info.get("title", "media")
            sanitized_title = sanitize_filename(title)
            filesize_bytes = (
                info.get("filesize")
                or info.get("filesize_approx")
                or info.get("requested_downloads", [{}])[0].get("filesize_approx")
            )
            filesize_mb = round(filesize_bytes / (1024 * 1024), 2) if filesize_bytes else "Unknown"

        output_filename = f"{sanitized_title}.{file_format}"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

        if file_format == "mp3":
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': output_path,
                'no-part': True,
                'cookiefile': 'cookies.txt',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': True
            }
        else:
            ydl_opts = {
                'format': 'bestvideo+bestaudio/best',
                'outtmpl': output_path,
                'merge_output_format': 'mp4',
                'cookiefile': 'cookies.txt',
                'quiet': True
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        delete_file_later(output_path)

        full_url = request.host_url.rstrip('/') + f"/download/{secure_filename(output_filename)}"
        return jsonify({
            "title": title,
            "duration_seconds": duration,
            "estimated_size_mb": filesize_mb,
            "download_url": full_url
        })

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download error: {e}")
        return jsonify({"error": "Download failed. The video may be private, require login, or be unsupported."}), 400
    except Exception as e:
        logger.exception("Unexpected error")
        return jsonify({"error": "An error occurred: " + str(e)}), 500

@app.route("/download/<filename>")
def download(filename):
    safe_filename = secure_filename(filename)
    return send_from_directory(DOWNLOAD_FOLDER, safe_filename)
