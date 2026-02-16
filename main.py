import os
import uuid
import shutil
import subprocess
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()


# ==========================
# Request Model
# ==========================
class RenderRequest(BaseModel):
    video_urls: List[str]
    audio_url: str


# ==========================
# Utils
# ==========================

def run_cmd(cmd: List[str]):
    """Run subprocess command safely"""
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if result.returncode != 0:
        raise Exception(result.stderr)
    return result.stdout


def download_file(url: str, output_path: str):
    """Download file using curl (simpler inside Railway container)"""
    cmd = [
        "curl",
        "-L",
        url,
        "-o",
        output_path
    ]
    run_cmd(cmd)


def concat_videos(video_paths: List[str], output_path: str):
    """
    Concat videos using FFmpeg concat demuxer
    Handles safe quoting properly (FIXED VERSION)
    """
    list_file = f"/tmp/{uuid.uuid4()}.txt"

    with open(list_file, "w") as f:
        for p in video_paths:
            # 🔥 FIXED (no backslash inside f-string expression)
            escaped = p.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    cmd = [
        "ffmpeg",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        output_path
    ]

    run_cmd(cmd)

    os.remove(list_file)


def merge_audio(video_path: str, audio_path: str, output_path: str):
    """
    Merge audio into final video
    """
    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        output_path
    ]

    run_cmd(cmd)


# ==========================
# API Endpoint
# ==========================

@app.post("/render")
def render_video(req: RenderRequest):

    if not req.video_urls:
        raise HTTPException(status_code=400, detail="No video URLs provided")

    work_id = str(uuid.uuid4())
    work_dir = f"/tmp/{work_id}"
    os.makedirs(work_dir, exist_ok=True)

    try:
        # --------------------------
        # 1️⃣ Download videos
        # --------------------------
        video_files = []
        for i, url in enumerate(req.video_urls):
            path = f"{work_dir}/video_{i}.mp4"
            download_file(url, path)
            video_files.append(path)

        # --------------------------
        # 2️⃣ Download audio
        # --------------------------
        audio_path = f"{work_dir}/audio.mp3"
        download_file(req.audio_url, audio_path)

        # --------------------------
        # 3️⃣ Concat videos
        # --------------------------
        concat_path = f"{work_dir}/concat.mp4"
        concat_videos(video_files, concat_path)

        # --------------------------
        # 4️⃣ Merge audio
        # --------------------------
        final_output = f"{work_dir}/final.mp4"
        merge_audio(concat_path, audio_path, final_output)

        # --------------------------
        # 5️⃣ Return response
        # --------------------------
        return {
            "status": "success",
            "output_path": final_output
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # Cleanup (optional - comment out if debugging)
        pass
        # shutil.rmtree(work_dir, ignore_errors=True)
