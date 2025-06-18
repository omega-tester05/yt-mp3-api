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
import random
import json

app = Flask(__name__)
CORS(app)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_DURATION_SECONDS = 150 * 60
MAX_FILENAME_LENGTH = 128

# Rate limiter
def get_user_id():
    return str(request.get_json().get("user_id", "anonymous")) if request.is_json else "anonymous"

limiter = Limiter(
    key_func=get_user_id,
    app=app,
    default_limits=["30 per hour"],
    storage_uri="memory://"
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"
]

PROXIES = os.getenv('PROXIES', '').split(',') if os.getenv('PROXIES') else None

def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("FFmpeg check passed")
    except Exception as e:
        logger.error(f"FFmpeg check failed: {str(e)}")
        raise EnvironmentError("FFmpeg is not installed or not in system PATH.")

def get_random_user_agent():
    return random.choice(USER_AGENTS)

def get_random_proxy():
    return random.choice(PROXIES) if PROXIES else None

def sanitize_filename(title):
    return re.sub(r'[\\/*?:"<>|]', '_', title)[:MAX_FILENAME_LENGTH]

def delete_file_later(filepath, delay=600):
    def delete():
        time.sleep(delay)
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Deleted file: {filepath}")
    threading.Thread(target=delete, daemon=True).start()

def get_ydl_opts(file_format, output_path, retry_count=0):
    opts = {
        'format': 'bestaudio/best' if file_format == "mp3" else 'bestvideo[height<=1080]+bestaudio/best',
        'outtmpl': output_path,
        'quiet': True,
        'retries': 3,
        'socket_timeout': 30 + retry_count * 10,
        'user_agent': get_random_user_agent(),
        'referer': 'https://www.youtube.com/',
        'proxy': get_random_proxy(),
        'throttled_rate': '500K',
        'extractor_args': {'youtube': {'skip': ['hls', 'dash'], 'player_client': ['android', 'web']}},
        'compat_opts': {'youtube-skip-dash-manifest': True, 'no-youtube-unavailable-videos': True}
    }
    if file_format == "mp3":
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif file_format == "mp4":
        opts['merge_output_format'] = 'mp4'
    return opts

# Initialization check
try:
    check_ffmpeg()
except Exception as e:
    logger.critical(f"Startup failed: {str(e)}")
    raise

@app.route('/')
def index():
    return 'YT-MP3 API is live 🎶'

@app.route('/health')
def health():
    return "OK", 200

@app.route("/convert", methods=["POST"])
@limiter.limit("3 per minute")
def convert():
    if not request.is_json:
        return jsonify({"error": "JSON expected"}), 400

    data = request.get_json()
    url = data.get("url")
    file_format = data.get("format", "mp3").lower()

    if not url or not re.match(r'^https?://(www\.)?(youtube\.com|youtu\.be)/', url):
        return jsonify({"error": "Invalid YouTube URL"}), 400
    if file_format not in ["mp3", "mp4"]:
        return jsonify({"error": "Invalid format"}), 400

    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL({
                'quiet': True,
                'extract_flat': True,
                'user_agent': get_random_user_agent(),
                'proxy': get_random_proxy(),
                'socket_timeout': 30
            }) as ydl:
                info = ydl.extract_info(url, download=False)
                duration = info.get("duration", 0)
                if duration > MAX_DURATION_SECONDS:
                    return jsonify({"error": "Video too long"}), 400

                title = sanitize_filename(info.get("title", "media"))
                output_filename = f"{title}.{file_format}"
                output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

            time.sleep(random.uniform(1, 3))
            ydl_opts = get_ydl_opts(file_format, output_path, attempt)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            delete_file_later(output_path)
            download_url = request.url_root.rstrip('/') + '/download/' + secure_filename(output_filename)

            return jsonify({
                "title": title,
                "duration": duration,
                "download_url": download_url
            })

        except yt_dlp.utils.DownloadError as e:
            if "bot" in str(e).lower() or "429" in str(e):
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                return jsonify({"error": "YouTube temporary restriction", "code": "rate_limit"}), 429
            return jsonify({"error": "Download failed", "details": str(e)}), 400

        except Exception as e:
            if attempt < 2:
                time.sleep(5)
                continue
            return jsonify({"error": "Internal error", "details": str(e)}), 500

@app.route("/download/<filename>")
def download(filename):
    try:
        safe_filename = secure_filename(filename)
        return send_from_directory(
            DOWNLOAD_FOLDER,
            safe_filename,
            as_attachment=True,
            download_name=safe_filename
        )
    except FileNotFoundError:
        return jsonify({"error": "File expired or not found"}), 404
    except Exception as e:
        return jsonify({"error": "Download error", "details": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
