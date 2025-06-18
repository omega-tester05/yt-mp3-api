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
import random

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_DURATION_SECONDS = 150 * 60
MAX_FILENAME_LENGTH = 128

# Rate limiting configuration
def get_user_id():
    return str(request.get_json().get("user_id", "anonymous")) if request.is_json else "anonymous"

limiter = Limiter(
    key_func=get_user_id,
    app=app,
    default_limits=["30 per hour"],
    storage_uri="memory://"
)

# Rotating user agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
]

def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, 
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("FFmpeg check passed")
    except Exception as e:
        logger.error(f"FFmpeg check failed: {str(e)}")
        raise EnvironmentError("FFmpeg is not installed or not in system PATH.")

def get_random_user_agent():
    return random.choice(USER_AGENTS)

def sanitize_filename(title):
    clean = re.sub(r'[\\/*?:"<>|]', '_', title)
    return clean[:MAX_FILENAME_LENGTH]

def delete_file_later(filepath, delay=600):
    def delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"Successfully deleted file: {filepath}")
        except Exception as e:
            logger.error(f"Error deleting file {filepath}: {str(e)}")
    threading.Thread(target=delete, daemon=True).start()

def get_ydl_opts(file_format, output_path):
    """Configuration optimized for avoiding bot detection"""
    opts = {
        'format': 'bestaudio/best' if file_format == "mp3" else 'bestvideo[height<=1080]+bestaudio/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': False,
        'retries': 3,
        'socket_timeout': 30,
        'extract_flat': False,
        'user_agent': get_random_user_agent(),
        'referer': 'https://www.youtube.com/',
        'throttled_rate': '500K',
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
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif file_format == "mp4":
        opts['merge_output_format'] = 'mp4'
        
    return opts

# Initialize
try:
    check_ffmpeg()
    logger.info("Application initialized successfully")
except Exception as e:
    logger.critical(f"Initialization failed: {str(e)}")
    raise

@app.route('/')
def index():
    return 'YT-MP3 API is live 🎶'

@app.route("/convert", methods=["POST"])
@limiter.limit("3 per minute")
def convert():
    if not request.is_json:
        logger.warning("Received non-JSON request")
        return jsonify({"error": "JSON expected"}), 400

    data = request.get_json()
    url = data.get("url")
    file_format = data.get("format", "mp3").lower()

    if not url or not re.match(r'^https?://(www\.)?(youtube\.com|youtu\.be)/', url):
        logger.warning(f"Invalid YouTube URL received: {url}")
        return jsonify({"error": "Invalid YouTube URL"}), 400
    if file_format not in ["mp3", "mp4"]:
        logger.warning(f"Invalid format requested: {file_format}")
        return jsonify({"error": "Invalid format"}), 400

    try:
        logger.info(f"Processing request for URL: {url}")
        
        # Initial info extraction
        with yt_dlp.YoutubeDL({
            'quiet': True,
            'extract_flat': True,
            'user_agent': get_random_user_agent()
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get("duration", 0)
            if duration > MAX_DURATION_SECONDS:
                logger.warning(f"Video too long: {duration} seconds")
                return jsonify({"error": "Video too long"}), 400

            title = sanitize_filename(info.get("title", "media"))
            output_filename = f"{title}.{file_format}"
            output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

        # Random delay to mimic human behavior
        delay = random.uniform(1, 3)
        logger.debug(f"Waiting for {delay:.2f} seconds before download")
        time.sleep(delay)

        # Download with rotated settings
        ydl_opts = get_ydl_opts(file_format, output_path)
        logger.debug(f"Downloading with options: {ydl_opts}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        logger.info(f"Successfully downloaded: {output_filename}")
        delete_file_later(output_path)

        return jsonify({
            "title": title,
            "duration": duration,
            "download_url": f"{request.host_url.rstrip('/')}/download/{secure_filename(output_filename)}"
        })

    except yt_dlp.utils.DownloadError as e:
        error = str(e)
        logger.error(f"Download failed: {error}")
        
        if "bot" in error.lower() or "429" in error:
            return jsonify({"error": "Temporary restriction - try again later"}), 429
        return jsonify({"error": "Download failed"}), 400
        
    except Exception as e:
        logger.exception("Unexpected error in conversion")
        return jsonify({"error": "Internal error"}), 500

@app.route("/download/<filename>")
def download(filename):
    try:
        safe_filename = secure_filename(filename)
        logger.info(f"Serving download for: {safe_filename}")
        return send_from_directory(
            DOWNLOAD_FOLDER, 
            safe_filename, 
            as_attachment=True,
            download_name=safe_filename
        )
    except FileNotFoundError:
        logger.error(f"File not found: {filename}")
        return jsonify({"error": "File expired or not found"}), 404
    except Exception as e:
        logger.error(f"Error serving download: {str(e)}")
        return jsonify({"error": "Download error"}), 500

if __name__ == "__main__":
    try:
        logger.info("Starting application")
        app.run(host='0.0.0.0', port=5000)
    except Exception as e:
        logger.critical(f"Application failed to start: {str(e)}")
        raise