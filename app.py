from flask import Flask, request, jsonify, send_from_directory
import os
import yt_dlp
import re

app = Flask(__name__)
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

MAX_DURATION_SECONDS = 150 * 60  # 2 hours 30 minutes

def sanitize_filename(title):
    # Replace invalid characters with underscores
    return re.sub(r'[\\/*?:"<>|]', '_', title)

@app.route("/convert", methods=["POST"])
def convert():
    data = request.get_json()
    url = data.get("url")
    file_format = data.get("format", "mp3").lower()  # default format is mp3

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if file_format not in ["mp3", "mp4"]:
        return jsonify({"error": "Invalid format. Use 'mp3' or 'mp4'."}), 400

    try:
        # Step 1: Extract video metadata
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)

            duration = info.get("duration", 0)
            if duration > MAX_DURATION_SECONDS:
                return jsonify({"error": "Video exceeds maximum duration of 2 hours and 30 minutes."}), 400

            title = info.get("title", "media")
            sanitized_title = sanitize_filename(title)

            filesize_bytes = (
                info.get("filesize")
                or info.get("filesize_approx")
                or info.get("requested_downloads", [{}])[0].get("filesize_approx")
            )
            filesize_mb = round(filesize_bytes / (1024 * 1024), 2) if filesize_bytes else "Unknown"

        # Step 2: Define yt-dlp options
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
        else:  # mp4
            output_filename = f"{sanitized_title}.mp4"
            output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)
            ydl_opts = {
                'format': 'bestvideo+bestaudio/best',
                'outtmpl': output_path,
                'merge_output_format': 'mp4',
                'quiet': True
            }

        # Step 3: Download
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        return jsonify({
            "title": title,
            "duration_seconds": duration,
            "estimated_size_mb": filesize_mb,
            "download_url": f"/download/{output_filename}"
        })

    except yt_dlp.utils.DownloadError:
        return jsonify({"error": "Download failed. The video might be private, restricted, or not supported."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/download/<filename>")
def download(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
