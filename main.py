import os
import re
import json
import uuid
import shutil
import base64
import asyncio
import tempfile
import subprocess
from pathlib import Path
from typing import List, Optional

import requests
import edge_tts  # เพิ่มไลบรารี TTS
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

# ปรับเป็นแนวตั้งสำหรับ Shorts/TikTok
DEFAULT_FPS = int(os.getenv("VIDEO_FPS", "30"))
DEFAULT_WIDTH = int(os.getenv("VIDEO_WIDTH", "1080"))
DEFAULT_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "1920"))

GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
GCS_PREFIX = os.getenv("GCS_PREFIX", "renders/").strip()
GCS_PUBLIC = os.getenv("GCS_PUBLIC", "false").lower() in ("1", "true", "yes")
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()

PORT = int(os.environ.get("PORT", "8080"))

app = FastAPI(title=APP_NAME)

# -----------------------------
# Models
# -----------------------------
class SceneItem(BaseModel):
    scene_number: int = Field(..., ge=1)
    script: str
    image_base64: str

class RenderRequest(BaseModel):
    stock_symbol: str = "UNKNOWN"
    trade_setup: dict = {}
    data: List[SceneItem]
    output_name: Optional[str] = None
    return_file: Optional[bool] = False

# -----------------------------
# Helpers
# -----------------------------
def _run(cmd: List[str]) -> None:
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

def _save_base64_image(b64_str: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64_str))

async def _generate_audio(text: str, out_path: Path, voice: str = "th-TH-PremwadeeNeural") -> None:
    """ฟังก์ชันสร้างเสียงพากย์ภาษาไทยด้วย edge-tts"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # th-TH-PremwadeeNeural (เสียงผู้หญิง) หรือ th-TH-NiwatNeural (เสียงผู้ชาย)
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))

def _build_scene_video_from_image_and_audio(
    image_in: Path,
    audio_in: Path,
    out_path: Path,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
) -> None:
    """สร้างวิดีโอโดยยืดภาพนิ่งให้ยาวเท่ากับเสียงพากย์พอดี"""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", str(fps),
        "-i", str(image_in),
        "-i", str(audio_in), # นำเสียงพากย์เข้ามาประกอบ
        "-vf", vf,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest", # ท่าไม้ตาย: สั่งให้หยุดเรนเดอร์เมื่อไฟล์เสียงจบลง
        str(out_path),
    ]
    _run(cmd)

def _escape_concat_path(p: Path) -> str:
    s = str(p)
    return s.replace("'", "'\\''")

def _concat_videos(scene_paths: List[Path], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.parent / "concat_list.txt"

    lines = ["file '" + _escape_concat_path(p) + "'" for p in scene_paths]
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

# -----------------------------
# Routes
# -----------------------------
# สังเกตว่าเปลี่ยนเป็น async def เพื่อให้รองรับ edge-tts
@app.post("/render")
async def render(req: RenderRequest):
    if not req.data:
        raise HTTPException(status_code=400, detail="data array is empty")

    scenes = sorted(req.data, key=lambda s: s.scene_number)
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
            image_path = assets_dir / f"image_{s.scene_number:03d}.png"
            audio_path = assets_dir / f"audio_{s.scene_number:03d}.mp3"
            scene_out = scenes_dir / f"scene_{s.scene_number:03d}.mp4"

            # 1. บันทึกรูปจาก Base64
            _save_base64_image(s.image_base64, image_path)

            # 2. สร้างเสียงพากย์จาก Script (ตั้งค่าเสียงผู้หญิง th-TH-PremwadeeNeural)
            await _generate_audio(s.script, audio_path)

            # 3. ประกอบภาพและเสียงเข้าด้วยกัน
            _build_scene_video_from_image_and_audio(
                image_in=image_path,
                audio_in=audio_path,
                out_path=scene_out,
            )
            scene_mp4_paths.append(scene_out)

        base_name = req.output_name or f"{req.stock_symbol}_{uuid.uuid4().hex[:8]}.mp4"
        base_name = _safe_filename(base_name)
        if not base_name.lower().endswith(".mp4"):
            base_name += ".mp4"

        final_path = out_dir / base_name
        
        # 4. รวมทุกฉากเข้าด้วยกันเป็นคลิปเดียว
        _concat_videos(scene_mp4_paths, final_path)

        if req.return_file:
            return FileResponse(path=str(final_path), media_type="video/mp4", filename=base_name)

        return {"ok": True, "stock": req.stock_symbol, "local_file": str(final_path)}

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True)