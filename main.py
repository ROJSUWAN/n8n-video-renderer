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
import edge_tts
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont

# -----------------------------
# Google Cloud Storage Setup
# -----------------------------
try:
    from google.cloud import storage
except ImportError:
    storage = None

# -----------------------------
# Config & Environment Variables
# -----------------------------
APP_NAME = "n8n-video-renderer-pro"
DEFAULT_FPS = int(os.getenv("VIDEO_FPS", "30"))
DEFAULT_WIDTH = int(os.getenv("VIDEO_WIDTH", "1080"))
DEFAULT_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "1920"))

GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
GCS_PREFIX = os.getenv("GCS_PREFIX", "renders/").strip()
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()

app = FastAPI(title=APP_NAME)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print("\n" + "="*50, flush=True)
    print("🚨 เกิด Error 422: ข้อมูลที่ n8n ส่งมาไม่ตรง 🚨", flush=True)
    for error in exc.errors():
        print(f"  -> ตำแหน่ง: {error.get('loc')} | ปัญหา: {error.get('msg')}", flush=True)
    print("="*50 + "\n", flush=True)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

class SceneItem(BaseModel):
    scene_number: int
    script: str
    image_base64: str

class RenderRequest(BaseModel):
    stock_symbol: str = "UNKNOWN"
    trade_setup: dict = {}
    data: List[SceneItem]

# -----------------------------
# 🔤 Subtitle & Thai Word Wrap
# -----------------------------
FONT_PATH = "Sarabun-Bold.ttf"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/sarabun/Sarabun-Bold.ttf"

def get_font(fontsize):
    if not os.path.exists(FONT_PATH):
        try:
            r = requests.get(FONT_URL, allow_redirects=True, timeout=15)
            with open(FONT_PATH, 'wb') as f: f.write(r.content)
        except Exception:
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(FONT_PATH, fontsize)
    except Exception:
        return ImageFont.load_default()

def wrap_and_chunk_thai_text(text, max_chars_per_line=32, max_lines=3):
    """ตัดคำไทยให้สวยงาม และหั่นซับไตเติ้ลเป็นท่อนๆ (ท่อนละไม่เกิน 3 บรรทัด)"""
    try:
        from pythainlp.tokenize import word_tokenize
        words = word_tokenize(text, engine="newmm")
    except ImportError:
        words = list(text)

    chunks = []
    current_chunk = []
    current_line = ""

    for word in words:
        if len(current_line) + len(word) <= max_chars_per_line:
            current_line += word
        else:
            if current_line:
                current_chunk.append(current_line)
            current_line = word
            
            if len(current_chunk) == max_lines:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                
    if current_line:
        current_chunk.append(current_line)
    if current_chunk:
        chunks.append("\n".join(current_chunk))
        
    return chunks

def create_subtitle_image(text_chunk, out_path, width=1080, height=1920):
    try:
        scale_factor = width / 720.0 
        img = Image.new('RGBA', (width, height), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        font_size = int(28 * scale_factor)
        font = get_font(font_size)
        
        lines = text_chunk.split('\n')
        line_height = font_size + int(10 * scale_factor)
        total_height = len(lines) * line_height
        start_y = int(150 * scale_factor)
        rect_padding = int(15 * scale_factor)
        
        draw.rectangle(
            [20 * scale_factor, start_y - rect_padding, width - (20 * scale_factor), start_y + total_height + rect_padding], 
            fill=(0,0,0,160)
        )
        
        cur_y = start_y
        for line in lines:
            try:
                bbox = draw.textbbox((0, 0), line, font=font)
                text_width = bbox[2] - bbox[0]
            except AttributeError:
                text_width, _ = draw.textsize(line, font=font)
                
            x = (width - text_width) / 2
            draw.text((x-2, cur_y), line, font=font, fill="black")
            draw.text((x+2, cur_y), line, font=font, fill="black")
            draw.text((x, cur_y), line, font=font, fill="white")
            cur_y += line_height
            
        img.save(out_path)
    except Exception as e:
        print(f"❌ Subtitle Error: {e}", flush=True)
        Image.new('RGBA', (width, height), (0,0,0,0)).save(out_path)

# -----------------------------
# 🎬 Video Processing
# -----------------------------
def get_audio_duration(file_path):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        return float(result.stdout.strip())
    except:
        return 10.0 

def _run_ffmpeg(cmd: List[str]):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg Error: {proc.stderr}")

async def render_video_task(req: RenderRequest):
    workdir = Path(tempfile.mkdtemp(prefix="render_"))
    total_scenes = len(req.data)
    
    # 🔥 ค้นหาไฟล์โลโก้แบบกวาดทุก Path
    possible_logo_paths = [Path("my_logo.png"), Path(__file__).resolve().parent / "my_logo.png"]
    logo_actual_path = None
    for p in possible_logo_paths:
        if p.exists():
            logo_actual_path = p
            break
            
    has_logo = logo_actual_path is not None
    
    print(f"\n🎬 [START] เริ่มเรนเดอร์หุ้น {req.stock_symbol} | เจอโลโก้ไหม?: {has_logo} ({logo_actual_path})", flush=True)
    
    try:
        assets_dir = workdir / "assets"
        scenes_dir = workdir / "scenes"
        assets_dir.mkdir(parents=True); scenes_dir.mkdir(parents=True)

        scene_mp4s = []
        scenes = sorted(req.data, key=lambda s: s.scene_number)

        for s in scenes:
            print(f"\n⏳ [SCENE {s.scene_number}] กำลังประมวลผล...", flush=True)
            img_p = assets_dir / f"{s.scene_number}.png"
            aud_p = assets_dir / f"{s.scene_number}.mp3"
            scn_p = scenes_dir / f"{s.scene_number}.mp4"

            with open(img_p, "wb") as f:
                f.write(base64.b64decode(s.image_base64))

            tts = edge_tts.Communicate(s.script, "th-TH-PremwadeeNeural")
            await tts.save(str(aud_p))
            duration = get_audio_duration(aud_p)
            
            chunks = wrap_and_chunk_thai_text(s.script, max_chars_per_line=32, max_lines=3)
            total_chars = max(sum(len(c.replace('\n', '')) for c in chunks), 1)
            
            sub_inputs = []
            sub_filters = []
            current_time = 0.0
            
            for idx, chunk in enumerate(chunks):
                chunk_p = assets_dir / f"{s.scene_number}_sub_{idx}.png"
                create_subtitle_image(chunk, str(chunk_p), DEFAULT_WIDTH, DEFAULT_HEIGHT)
                sub_inputs.extend(["-i", str(chunk_p)])
                
                chunk_duration = (len(chunk.replace('\n', '')) / total_chars) * duration
                start_t = current_time
                end_t = current_time + chunk_duration
                current_time = end_t
                
                in_node = "[bg]" if idx == 0 else f"[v{idx}]"
                is_last = (idx == len(chunks) - 1)
                
                if is_last and not has_logo:
                    out_node = "[final_v]"
                elif is_last and has_logo:
                    out_node = "[final_sub]"
                else:
                    out_node = f"[v{idx+1}]"
                    
                sub_filters.append(f"{in_node}[{2+idx}:v]overlay=0:0:enable='between(t,{start_t:.3f},{end_t:.3f})'{out_node}")

            print(f"   -> 🎞️ เรนเดอร์วิดีโอ (ความยาว {duration:.2f} วิ | ซับ {len(chunks)} สไลด์)...", flush=True)
            
            cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(DEFAULT_FPS), "-i", str(img_p), "-i", str(aud_p)] + sub_inputs
            
            fc_parts = [f"[0:v]scale={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:force_original_aspect_ratio=decrease,pad={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={DEFAULT_FPS}[bg]"]
            fc_parts.extend(sub_filters)
            
            if has_logo:
                cmd.extend(["-i", str(logo_actual_path)])
                logo_idx = 2 + len(chunks)
                logo_width = int(200 * (DEFAULT_WIDTH / 720.0))
                # 🔥 บังคับ format=rgba เพื่อดึงความโปร่งใส ป้องกันไฟล์ PNG มีปัญหา
                fc_parts.append(f"[{logo_idx}:v]format=rgba,scale={logo_width}:-1,colorchannelmixer=aa=0.9[logo]")
                fc_parts.append(f"[final_sub][logo]overlay=W-w-30:30[final_v]")

            filter_complex = ";".join(fc_parts)
            
            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "[final_v]", "-map", "1:a",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23", 
                "-c:a", "aac", "-b:a", "128k",
                "-r", str(DEFAULT_FPS),
                "-t", str(duration), 
                str(scn_p)
            ])
            _run_ffmpeg(cmd)
            scene_mp4s.append(scn_p)

        print(f"\n🔗 [CONCAT] รวมไฟล์วิดีโอ...", flush=True)
        final_name = f"{req.stock_symbol}_{uuid.uuid4().hex[:6]}.mp4"
        final_path = workdir / final_name
        list_p = workdir / "list.txt"
        list_p.write_text("\n".join([f"file '{str(p.absolute())}'" for p in scene_mp4s]))
        
        _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_p), "-c", "copy", str(final_path)])

        if storage and GCS_BUCKET:
            print(f"☁️ [UPLOAD] อัปโหลดขึ้น GCS...", flush=True)
            client = storage.Client.from_service_account_info(json.loads(GCP_SA_JSON)) if GCP_SA_JSON else storage.Client()
            blob = client.bucket(GCS_BUCKET).blob(f"{GCS_PREFIX}{final_name}")
            blob.upload_from_filename(str(final_path))
            print(f"🎉 สำเร็จ! URL: https://storage.googleapis.com/{GCS_BUCKET}/{GCS_PREFIX}{final_name}\n", flush=True)

    except Exception as e:
        print(f"\n❌ [ERROR]: {str(e)}\n", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

@app.post("/render")
async def create_render_job(req: RenderRequest, background_tasks: BackgroundTasks):
    if not req.data: raise HTTPException(status_code=400, detail="No scene data provided")
    background_tasks.add_task(render_video_task, req)
    return {"status": "accepted", "message": f"Rendering started.", "bucket": GCS_BUCKET}

@app.get("/health")
def health(): return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))