import os
import re
import json
import uuid
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Optional: GCS upload
try:
    from google.cloud import storage  # type: ignore
except Exception:
    storage = None  # if not installed, upload will be disabled


# -----------------------------
# Config
# -----------------------------
APP_NAME = "n8n-video-renderer"
DEFAULT_FPS = int(os.getenv("VIDEO_FPS", "30"))
DEFAULT_WIDTH = int(os.getenv("VIDEO_WIDTH", "1280"))
DEFAULT_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "720"))

# If you want auto-upload to GCS, set:
#   GCS_BUCKET=your-bucket
# Optional:
#   GCS_PREFIX=videos/
#   GCS_PUBLIC=true  (make public)
GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
GCS_PREFIX = os.getenv("GCS_PREFIX", "renders/").strip()
GCS_PUBLIC = os.getenv("GCS_PUBLIC", "false").lower() in ("1", "true", "yes")

# If you keep service account json in env:
#   GOOGLE_APPLICATION_CREDENTIALS=/app/sa.json   (file path) OR
#   GCP_SA_JSON='{"type": "..."}'  (json string)
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()

# Railway uses PORT env var
PORT = int(os.environ.get("PORT", "8080"))

app = FastAPI(title=APP_NAME)


# -----------------------------
# Models
# -----------------------------
class Scene(BaseModel):
    scene_order: int = Field(..., ge=1)
    duration_sec: int = Field(..., ge=1, le=600)
    pixabay_video_url: str
    tts_audio_url: str
    script: Optional[str] = None


class RenderRequest(BaseModel):
    project_id: str
    topic: Optional[str] = None
    style: Optional[str] = None
    language: Optional[str] = None
    duration_min: Optional[int] = None
    scenes: List[Scene]
    # If you want override output name:
    output_name: Optional[str] = None
    # If true, return file directly instead of URL
    return_file: Optional[bool] = False


# -----------------------------
# Helpers
# -----------------------------
def _run(cmd: List[str]) -> None:
    """Run command and raise on error."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


def _safe_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    return name[:180] if name else str(uuid.uuid4())


def _download(url: str, out_path: Path, timeout: int = 60) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def _build_scene_video(
    video_in: Path,
    audio_in: Path,
    duration_sec: int,
    out_path: Path,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
) -> None:
    """
    Create a scene mp4:
    - Trim video to duration
    - Scale to width/height
    - Mux with narration audio
    - Stop at shortest stream
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Re-encode for consistent concat
    # - Force yuv420p, aac, baseline-friendly
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_in),
        "-i", str(audio_in),
        "-t", str(duration_sec),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(out_path),
    ]
    _run(cmd)


def _concat_videos(scene_paths: List[Path], out_path: Path) -> None:
    """
    Concat mp4 files using concat demuxer.
    Note: files should have same codec/params -> we ensured in _build_scene_video.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.parent / "concat_list.txt"

    # IMPORTANT: escape single quotes for ffmpeg concat list
    lines = []
    for p in scene_paths:
        # fix: do NOT do replace with backslash inside f-string expression
        safe_p = str(p).replace("'", "'\\''")
        lines.append("file '" + safe_p + "'")

    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ]
    _run(cmd)


def _gcs_client():
    if storage is None:
        raise RuntimeError("google-cloud-storage is not installed. Add it to requirements.txt")
    # If service account json provided in env, write it to temp and load
    if GCP_SA_JSON:
        data = json.loads(GCP_SA_JSON)
        # create client from info
        return storage.Client.from_service_account_info(data)
    # else rely on GOOGLE_APPLICATION_CREDENTIALS / default creds
    return storage.Client()


def _upload_to_gcs(local_path: Path, object_name: str) -> str:
    if not GCS_BUCKET:
        raise RuntimeError("GCS_BUCKET is not set")

    client = _gcs_client()
    bucket = client.bucket(GCS_BUCKET)

    obj = f"{GCS_PREFIX.rstrip('/')}/{object_name}".lstrip("/")
    blob = bucket.blob(obj)
    blob.upload_from_filename(str(local_path), content_type="video/mp4")

    if GCS_PUBLIC:
        blob.make_public()
        return blob.public_url

    # If not public, return a signed URL (default 1 hour)
    # NOTE: requires service account credentials
    url = blob.generate_signed_url(expiration=3600, method="GET")
    return url


# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME}


@app.post("/render")
def render(req: RenderRequest):
    if not req.scenes:
        raise HTTPException(status_code=400, detail="scenes is empty")

    # Sort scenes by scene_order
    scenes = sorted(req.scenes, key=lambda s: s.scene_order)

    workdir = Path(tempfile.mkdtemp(prefix="render_"))
    try:
        assets_dir = workdir / "assets"
        scenes_dir = workdir / "scenes"
        out_dir = workdir / "out"
        assets_dir.mkdir(parents=True, exist_ok=True)
        scenes_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        scene_mp4_paths: List[Path] = []

        for s in scenes:
            video_path = assets_dir / f"video_{s.scene_order:03d}.mp4"
            audio_path = assets_dir / f"audio_{s.scene_order:03d}.mp3"
            scene_out = scenes_dir / f"scene_{s.scene_order:03d}.mp4"

            _download(s.pixabay_video_url, video_path)
            _download(s.tts_audio_url, audio_path)

            _build_scene_video(
                video_in=video_path,
                audio_in=audio_path,
                duration_sec=int(s.duration_sec),
                out_path=scene_out,
            )
            scene_mp4_paths.append(scene_out)

        # final output name
        base_name = req.output_name or f"{req.project_id}_{uuid.uuid4().hex[:8]}.mp4"
        base_name = _safe_filename(base_name)
        if not base_name.lower().endswith(".mp4"):
            base_name += ".mp4"

        final_path = out_dir / base_name
        _concat_videos(scene_mp4_paths, final_path)

        # If user wants file directly
        if req.return_file:
            return FileResponse(
                path=str(final_path),
                media_type="video/mp4",
                filename=base_name,
            )

        # else upload to GCS if configured, otherwise return local path (for debugging)
        if GCS_BUCKET:
            url = _upload_to_gcs(final_path, base_name)
            return {"ok": True, "project_id": req.project_id, "output_url": url, "file": base_name}

        return {"ok": True, "project_id": req.project_id, "local_file": str(final_path)}

    except requests.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    finally:
        # cleanup
        shutil.rmtree(workdir, ignore_errors=True)


# -----------------------------
# Entry
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
