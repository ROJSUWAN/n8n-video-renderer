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

# 👉 ไลบรารีสำหรับสร้างภาพซับไตเติ้ล
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

# กำหนดชื่อไฟล์โลโก้
LOGO_PATH = "my_logo.png"

app = FastAPI(title=APP_NAME)

# -----------------------------
# 🚨 ตัวดักจับ 422 Error
# -----------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print("\n" + "="*50, flush=True)
    print("🚨 เกิด Error 422: ข้อมูลที่ n8n ส่งมาไม่ตรง 🚨", flush=True)
    for error in exc.errors():
        print(f"  -> ตำแหน่ง: {error.get('loc')} | ปัญหา: {error.get('msg')}", flush=True)
    print("="*50 + "\n", flush=True)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# -----------------------------
# Models
# -----------------------------
class SceneItem(BaseModel):
    scene_number: int
    script: str
    image_base64: str

class RenderRequest(BaseModel):
    stock_symbol: str = "UNKNOWN"
    trade_setup: dict = {}
    data: List[SceneItem]

# -----------------------------
# 🔤 Subtitle Generation Functions
# -----------------------------
FONT_PATH = "Sarabun-Bold.ttf"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/sarabun/Sarabun-Bold.ttf"

def get_font(fontsize):
    """โหลดฟอนต์ภาษาไทยอัตโนมัติจาก Google Fonts"""
    # 1. เช็คว่ามีไฟล์ฟอนต์บนเซิร์ฟเวอร์หรือยัง ถ้ายังให้ดาวน์โหลด
    if not os.path.exists(FONT_PATH):
        print(f"📥 กำลังดาวน์โหลดฟอนต์ภาษาไทย ({FONT_PATH})...", flush=True)
        try:
            r = requests.get(FONT_URL, allow_redirects=True, timeout=15)
            with open(FONT_PATH, 'wb') as f:
                f.write(r.content)
            print(f"✅ ดาวน์โหลดฟอนต์สำเร็จ!", flush=True)
        except Exception as e:
            print(f"❌ โหลดฟอนต์ไม่สำเร็จ: {e}", flush=True)
            return ImageFont.load_default()
    
    # 2. นำฟอนต์มาใช้งาน
    try:
        return ImageFont.truetype(FONT_PATH, fontsize)
    except Exception as e:
        print(f"❌ ข้อผิดพลาดในการอ่านฟอนต์: {e}", flush=True)
        return ImageFont.load_default()

def create_subtitle_image(text, out_path, width=1080, height=1920):
    """สร้างภาพ PNG ซับไตเติ้ลพื้นหลังโปร่งใส เพื่อนำไปซ้อนในวิดีโอ"""
    try:
        scale_factor = width / 720.0 
        img = Image.new('RGBA', (width, height), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        
        font_size = int(28 * scale_factor)
        font = get_font(font_size)
        
        limit_chars = 40
        lines = []
        temp = ""
        for char in text:
            if len(temp) < limit_chars: temp += char
            else: lines.append(temp); temp = char
        if temp: lines.append(temp)
        
        line_height = font_size + int(10 * scale_factor)
        total_height = len(lines) * line_height
        
        start_y = int(150 * scale_factor) # ตำแหน่งด้านบน
        rect_padding = int(15 * scale_factor)
        
        # วาดพื้นหลังสีดำโปร่งแสงหลังข้อความ
        draw.rectangle(
            [20 * scale_factor, start_y - rect_padding, width - (20 * scale_factor), start_y + total_height + rect_padding], 
            fill=(0,0,0,160)
        )
        
        cur_y = start_y
        for line in lines:
            try: # รองรับ Pillow เวอร์ชั่นใหม่
                bbox = draw.textbbox((0, 0), line, font=font)
                text_width = bbox[2] - bbox[0]
            except AttributeError: # รองรับ Pillow เวอร์ชั่นเก่า
                text_width, _ = draw.textsize(line, font=font)
                
            x = (width - text_width) / 2
            # ขอบดำและตัวหนังสือสีขาว
            draw.text((x-2, cur_y), line, font=font, fill="black")
            draw.text((x+2, cur_y), line, font=font, fill="black")
            draw.text((x, cur_y), line, font=font, fill="white")
            cur_y += line_height
            
        img.save(out_path)
    except Exception as e:
        print(f"❌ [SUBTITLE ERROR]: {e}", flush=True)
        Image.new('RGBA', (width, height), (0,0,0,0)).save(out_path)

# -----------------------------
# Video Processing Functions
# -----------------------------
def _run_ffmpeg(cmd: List[str]):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        print(f"❌ [FFMPEG ERROR]: {proc.stderr}", flush=True)
        raise RuntimeError(f"FFmpeg Error: {proc.stderr}")

async def render_video_task(req: RenderRequest):
    """ฟังก์ชันหลักที่ทำงานเบื้องหลัง"""
    workdir = Path(tempfile.mkdtemp(prefix="render_"))
    total_scenes = len(req.data)
    has_logo = os.path.exists(LOGO_PATH)
    
    print(f"\n🎬 [START] เริ่มต้นงานเรนเดอร์วิดีโอหุ้น: {req.stock_symbol} จำนวน {total_scenes} ฉาก", flush=True)
    print(f"📁 สร้างพื้นที่ทำงานชั่วคราว: {workdir}", flush=True)
    
    try:
        assets_dir = workdir / "assets"
        scenes_dir = workdir / "scenes"
        assets_dir.mkdir(parents=True); scenes_dir.mkdir(parents=True)

        scene_mp4s = []
        scenes = sorted(req.data, key=lambda s: s.scene_number)

        for s in scenes:
            print(f"\n⏳ [SCENE {s.scene_number}/{total_scenes}] เริ่มประมวลผลฉากที่ {s.scene_number}...", flush=True)
            
            img_p = assets_dir / f"{s.scene_number}.png"
            aud_p = assets_dir / f"{s.scene_number}.mp3"
            sub_p = assets_dir / f"{s.scene_number}_sub.png"
            scn_p = scenes_dir / f"{s.scene_number}.mp4"

            # 1. Save Image
            print(f"   -> 🖼️ กำลังบันทึกรูปภาพ...", flush=True)
            with open(img_p, "wb") as f:
                f.write(base64.b64decode(s.image_base64))

            # 2. Generate Audio (Thai Voice)
            print(f"   -> 🎙️ กำลังดึงเสียงพากย์ AI (TTS)...", flush=True)
            tts = edge_tts.Communicate(s.script, "th-TH-PremwadeeNeural")
            await tts.save(str(aud_p))
            
            # 3. Generate Subtitle Image
            print(f"   -> 🔤 กำลังสร้างภาพซับไตเติ้ลภาษาไทย...", flush=True)
            create_subtitle_image(s.script, str(sub_p), width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT)

            # 4. Build Scene Video (FFmpeg + Filters)
            print(f"   -> 🎞️ กำลังเรนเดอร์ประกอบภาพ เสียง ซับไตเติ้ล {'และโลโก้ ' if has_logo else ''}(FFmpeg)...", flush=True)
            
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-framerate", str(DEFAULT_FPS),
                "-i", str(img_p),
                "-i", str(aud_p),
                "-i", str(sub_p)
            ]
            
            # ชุดคำสั่งประกอบร่าง (ซ้อนภาพ, ซ้อนซับ, ซ้อนโลโก้)
            fc_parts = []
            fc_parts.append(f"[0:v]scale={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:force_original_aspect_ratio=decrease,pad={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={DEFAULT_FPS}[bg]")
            
            if has_logo:
                cmd.extend(["-i", LOGO_PATH])
                logo_width = int(200 * (DEFAULT_WIDTH / 720.0))
                fc_parts.append(f"[bg][2:v]overlay=0:0[with_sub]")
                fc_parts.append(f"[3:v]scale={logo_width}:-1,colorchannelmixer=aa=0.9[logo]")
                fc_parts.append(f"[with_sub][logo]overlay=W-w-30:30[final_v]")
            else:
                fc_parts.append(f"[bg][2:v]overlay=0:0[final_v]")
                
            filter_complex = ";".join(fc_parts)
            
            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "[final_v]",
                "-map", "1:a",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-shortest", str(scn_p)
            ])
            
            _run_ffmpeg(cmd)
            scene_mp4s.append(scn_p)
            print(f"   ✅ ฉากที่ {s.scene_number} เสร็จสมบูรณ์!", flush=True)

        # 5. Concat all scenes
        print(f"\n🔗 [CONCAT] กำลังรวมวิดีโอทั้ง {total_scenes} ฉากเข้าด้วยกัน...", flush=True)
        final_name = f"{req.stock_symbol}_{uuid.uuid4().hex[:6]}.mp4"
        final_path = workdir / final_name
        list_p = workdir / "list.txt"
        list_p.write_text("\n".join([f"file '{str(p.absolute())}'" for p in scene_mp4s]))
        
        _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_p), "-c", "copy", str(final_path)])
        print(f"✅ [CONCAT] รวมไฟล์เสร็จสมบูรณ์ -> {final_name}", flush=True)

        # 6. Upload to Google Cloud Storage
        print(f"\n☁️ [UPLOAD] กำลังอัปโหลดขึ้น Google Cloud Storage (Bucket: {GCS_BUCKET})...", flush=True)
        if storage and GCS_BUCKET:
            if GCP_SA_JSON:
                client = storage.Client.from_service_account_info(json.loads(GCP_SA_JSON))
            else:
                client = storage.Client()
            
            bucket = client.bucket(GCS_BUCKET)
            blob = bucket.blob(f"{GCS_PREFIX}{final_name}")
            blob.upload_from_filename(str(final_path))
            
            print(f"🎉 [SUCCESS] อัปโหลดสำเร็จ! วิดีโอของคุณพร้อมใช้งานแล้ว", flush=True)
            print(f"🌐 URL: https://storage.googleapis.com/{GCS_BUCKET}/{GCS_PREFIX}{final_name}\n", flush=True)
        else:
            print("⚠️ [WARNING] ข้ามการอัปโหลด GCS เนื่องจากไม่ได้ตั้งค่าตัวแปร GCS_BUCKET", flush=True)

    except Exception as e:
        print(f"\n❌ [ERROR] เกิดข้อผิดพลาดร้ายแรงระหว่างเรนเดอร์หุ้น {req.stock_symbol}: {str(e)}\n", flush=True)
    finally:
        print("🧹 [CLEANUP] ลบไฟล์ชั่วคราวทิ้งเพื่อคืนพื้นที่ให้เซิร์ฟเวอร์...", flush=True)
        shutil.rmtree(workdir, ignore_errors=True)
        print("="*50 + "\n", flush=True)

# -----------------------------
# API Endpoints
# -----------------------------
@app.post("/render")
async def create_render_job(req: RenderRequest, background_tasks: BackgroundTasks):
    if not req.data:
        raise HTTPException(status_code=400, detail="No scene data provided")

    background_tasks.add_task(render_video_task, req)

    print(f"📩 [API] ได้รับคำสั่งเรนเดอร์หุ้น {req.stock_symbol} ตอบ 200 OK ให้ n8n กลับไปทำงานต่อ", flush=True)
    return {
        "status": "accepted",
        "message": f"Rendering job for {req.stock_symbol} started.",
        "bucket": GCS_BUCKET
    }

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))