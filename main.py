import os
import re
import json
import uuid
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import List, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
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
#   GCS_PREFIX=renders/
#   GCS_PUBLIC=true  (make public)
GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
GCS_PREFIX = os.getenv("GCS_PREFIX", "renders/").strip()
GCS_PUBLIC = os.getenv("GCS_PUBLIC", "false").lower() in ("1", "true", "yes")

# If you keep service account json in env:
#   GCP_SA_JSON='{"type": "..."}'  (json string)
# or rely on GOOGLE_APPLICATION_CREDENTIALS / workload identity
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()

# Railway uses PORT env var
PORT = int(os.environ.get("PORT", "8080"))

# Requests defaults
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT_SEC", "90"))
DOWNLOAD_RETRIES = int(os.getenv("DOWNLOAD_RETRIES", "2"))


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
    output_name: Optional[str] = None
    return_file: Optional[bool] = False


# -----------------------------
# Helpers
# -----------------------------
def _run(cmd: List[str]) -> None:
    """Run command and raise on error."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"CMD: {' '.join(cmd)}\n\n"
            f"STDOUT:\n{proc.stdout}\n\n"
            f"STDERR:\n{proc.stderr}\n"
        )


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    return name[:180] if name else f"{uuid.uuid4().hex}.mp4"


def _download(url: str, out_path: Path, timeout: int = DOWNLOAD_TIMEOUT, retries: int = DOWNLOAD_RETRIES) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not (url.startswith("http://") or url.startswith("https://")):
        raise RuntimeError(f"Invalid URL scheme: {url}")

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                continue
            raise RuntimeError(f"Download failed after {retries+1} attempts: {url} | err={e}") from e

    # should never reach
    if last_err:
        raise RuntimeError(str(last_err))


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
    - Trim to duration
    - Scale/pad to width/height
    - Mux with narration audio
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_in),
        "-i", str(audio_in),
        "-t", str(int(duration_sec)),
        "-vf", vf,
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


def _escape_concat_path(p: Path) -> str:
    """
    ffmpeg concat demuxer line format: file 'path'
    If path contains single quote -> escape as: '\''
    """
    s = str(p)
    return s.replace("'", "'\\''")


def _concat_videos(scene_paths: List[Path], out_path: Path) -> None:
    """
    Concat mp4 files using concat demuxer.
    Note: files should have same codec/params -> ensured in _build_scene_video.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.parent / "concat_list.txt"

    lines = []
    for p in scene_paths:
        lines.append("file '" + _escape_concat_path(p) + "'")

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
    if GCP_SA_JSON:
        data = json.loads(GCP_SA_JSON)
        return storage.Client.from_service_account_info(data)
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

    # Signed URL (1 hour). Requires service account creds.
    return blob.generate_signed_url(expiration=3600, method="GET")


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    # IMPORTANT: Railway/ingress health probes often hit "/" first.
    return {"ok": True, "app": APP_NAME, "port": PORT}


@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME}


@app.post("/render")
def render(req: RenderRequest):
    if not req.scenes:
        raise HTTPException(status_code=400, detail="scenes is empty")

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

        base_name = req.output_name or f"{req.project_id}_{uuid.uuid4().hex[:8]}.mp4"
        base_name = _safe_filename(base_name)
        if not base_name.lower().endswith(".mp4"):
            base_name += ".mp4"

        final_path = out_dir / base_name
        _concat_videos(scene_mp4_paths, final_path)

        if req.return_file:
            return FileResponse(path=str(final_path), media_type="video/mp4", filename=base_name)

        if GCS_BUCKET:
            url = _upload_to_gcs(final_path, base_name)
            return {"ok": True, "project_id": req.project_id, "output_url": url, "file": base_name}

        # fallback (debug)
        return {"ok": True, "project_id": req.project_id, "local_file": str(final_path)}

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -----------------------------
# Entry
# -----------------------------
if __name__ == "__main__":
    import uvicorn

    # proxy_headers helps when behind Railway ingress
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True)
