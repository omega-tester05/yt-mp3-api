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

app = Flask(__name__)
CORS(app)  # Enable CORS for all domains (for Android/web frontend)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

MAX_DURATION_SECONDS = 150 * 60  # 2 hours 30 minutes

# 🔒 Custom limiter key based on user_id
def get_user_id():
    if request.method == "POST" and request.is_json:
        user_id = request.get_json().get("user_id")
        if user_id:
            return str(user_id)
    return "anonymous"

# ⚠️ Limit per user_id
limiter = Limiter(
    key_func=get_user_id,
    app=app,
    default_limits=["10 per hour"]
)

# ✅ Check FFmpeg presence
def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        raise EnvironmentError("FFmpeg is not installed or not in system PATH.")
check_ffmpeg()

# ✅ Clean filename
def sanitize_filename(title):
    return re.sub(r'[\\/*?:"<>|]', '_', title)

# ✅ Auto delete downloaded file
def delete_file_later(filepath, delay=600):
    def delete():
        time.sleep(delay)
        if os.path.exists(filepath):
            os.remove(filepath)
    threading.Thread(target=delete).start()

# ✅ Root route for Render homepage
@app.route('/')
def index():
    return 'YT-MP3 API is live 🎶'    

# ✅ Conversion Route
@app.route("/convert", methods=["POST"])
@limiter.limit("3 per minute")  # Per user_id
def convert():
    if not request.is_json:
        return jsonify({"error": "Invalid request format. JSON expected."}), 400

    data = request.get_json()
    url = data.get("url")
    file_format = data.get("format", "mp3").lower()
    user_id = data.get("user_id")

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not user_id:
        return jsonify({"error": "Missing user_id in request."}), 400
    if file_format not in ["mp3", "mp4"]:
        return jsonify({"error": "Invalid format. Use 'mp3' or 'mp4'."}), 400

    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
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

        if file_format == "mp3":
            output_filename = f"{sanitized_title}.mp3"
            output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': output_path,
                'no-part': True,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': True
            }
        else:
            output_filename = f"{sanitized_title}.mp4"
            output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)
            ydl_opts = {
                'format': 'bestvideo+bestaudio/best',
                'outtmpl': output_path,
                'merge_output_format': 'mp4',
                'quiet': True
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        delete_file_later(output_path)  # Clean up after 10 min

        full_url = request.host_url.rstrip('/') + f"/download/{secure_filename(output_filename)}"

        return jsonify({
            "title": title,
            "duration_seconds": duration,
            "estimated_size_mb": filesize_mb,
            "download_url": full_url
        })

    except yt_dlp.utils.DownloadError:
        return jsonify({"error": "Download failed. The video may be private or unsupported."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ✅ Safe file download
@app.route("/download/<filename>")
def download(filename):
    safe_filename = secure_filename(filename)
    return send_from_directory(DOWNLOAD_FOLDER, safe_filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
