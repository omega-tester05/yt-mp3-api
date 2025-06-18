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
from datetime import datetime

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_DURATION_SECONDS = 150 * 60  # 2.5 hours
MAX_FILENAME_LENGTH = 128
COOKIES_FILE = "cookies.txt"

# Rate limiting per user_id
def get_user_id():
    if request.method == "POST" and request.is_json:
        return str(request.get_json().get("user_id", "anonymous"))
    return "anonymous"

limiter = Limiter(
    key_func=get_user_id,
    app=app,
    default_limits=["30 per hour"],
    storage_uri="memory://"
)

def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        logger.error("FFmpeg is missing or not in PATH.")
        raise EnvironmentError("FFmpeg is not installed or not in system PATH.")

def init_cookies():
    """Initialize or validate cookies file"""
    if not os.path.exists(COOKIES_FILE):
        logger.warning(f"{COOKIES_FILE} not found! YouTube may block requests")
        if os.getenv('COOKIES_CONTENT'):
            with open(COOKIES_FILE, 'w') as f:
                f.write(os.getenv('COOKIES_CONTENT'))
            logger.info("Created cookies.txt from environment variable")
    else:
        # Check if cookies are expired
        cookie_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(COOKIES_FILE))
        if cookie_age.days > 7:  # Cookies typically expire after 1 week
            logger.warning("Cookies file is older than 7 days - may be expired")

def sanitize_filename(title):
    clean = re.sub(r'[\\/*?:"<>|]', '_', title)
    return clean[:MAX_FILENAME_LENGTH]

def delete_file_later(filepath, delay=600):
    def delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"Deleted: {filepath}")
        except Exception as e:
            logger.error(f"Error deleting file {filepath}: {e}")
    threading.Thread(target=delete, daemon=True).start()

def get_ydl_opts(file_format, output_path):
    """Enhanced YouTube DL options with anti-bot measures"""
    base_options = {
        'cookiefile': COOKIES_FILE,
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': False,
        'ignoreerrors': False,
        'retries': 3,
        'extract_flat': False,
        # Anti-bot measures
        'referer': 'https://www.youtube.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'socket_timeout': 30,
        'throttled_rate': '1M',  # Limit download speed
        'extractor_args': {
            'youtube': {
                'skip': ['hls', 'dash'],
                'player_client': ['android', 'web'],
            }
        },
        'compat_opts': {
            'youtube-skip-dash-manifest': True,
            'no-youtube-unavailable-videos': True,
        }
    }

    if file_format == "mp3":
        base_options.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        base_options.update({
            'format': 'bestvideo[height<=1080]+bestaudio/best',
            'merge_output_format': 'mp4',
        })

    return base_options

# Initialize required components
check_ffmpeg()
init_cookies()

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
        # Initial info extraction with simple options
        with yt_dlp.YoutubeDL({
            'quiet': True,
            'cookiefile': COOKIES_FILE,
            'extract_flat': True
        }) as ydl:
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

        # Add small delay to avoid rate limiting
        time.sleep(2)

        # Actual download with enhanced options
        ydl_opts = get_ydl_opts(file_format, output_path)
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
        error_msg = str(e)
        logger.error(f"Download error: {error_msg}")
        
        if "Sign in to confirm you're not a bot" in error_msg:
            return jsonify({
                "error": "YouTube bot protection triggered. Try again later or update cookies.",
                "code": "bot_detected"
            }), 429
        elif "HTTP Error 429" in error_msg:
            return jsonify({
                "error": "YouTube rate limit exceeded. Try again later.",
                "code": "rate_limit"
            }), 429
        else:
            return jsonify({
                "error": "Download failed. The video may be private or unavailable.",
                "details": error_msg
            }), 400
            
    except Exception as e:
        logger.exception("Unexpected error in conversion")
        return jsonify({
            "error": "An internal error occurred",
            "details": str(e)
        }), 500

@app.route("/download/<filename>")
def download(filename):
    safe_filename = secure_filename(filename)
    try:
        return send_from_directory(DOWNLOAD_FOLDER, safe_filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify({"error": "File not found or expired"}), 404

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)