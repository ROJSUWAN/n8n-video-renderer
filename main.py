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
    print("🚨 [ERROR 422] ข้อมูลที่ n8n ส่งมาไม่ตรงกับโครงสร้างที่กำหนด 🚨", flush=True)
    for err in exc.errors():
        print(f"  -> ตำแหน่ง: {err.get('loc')} | ปัญหา: {err.get('msg')}", flush=True)
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
# 🖼️ Auto-Download Logo
# -----------------------------
LOGO_PATH = "my_logo.png"
LOGO_URL = "https://raw.githubusercontent.com/ROJSUWAN/n8n-video-renderer/main/my_logo.png"

def setup_logo():
    if not os.path.exists(LOGO_PATH):
        print("📥 [INIT] กำลังดาวน์โหลดโลโก้ my_logo.png จาก GitHub...", flush=True)
        try:
            r = requests.get(LOGO_URL, timeout=15)
            if r.status_code == 200:
                with open(LOGO_PATH, 'wb') as f:
                    f.write(r.content)
                print("✅ [INIT] โหลดโลโก้สำเร็จ!", flush=True)
            else:
                print(f"❌ [INIT] โหลดโลโก้ไม่ได้ (Status: {r.status_code})", flush=True)
        except Exception as e:
            print(f"❌ [INIT] โหลดโลโก้ไม่สำเร็จ: {e}", flush=True)
    return os.path.exists(LOGO_PATH)

# -----------------------------
# 🔤 Font & Text Utilities
# -----------------------------
FONT_PATH = "Sarabun-Bold.ttf"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/sarabun/Sarabun-Bold.ttf"

def get_font(fontsize):
    if not os.path.exists(FONT_PATH):
        print("📥 [INIT] กำลังดาวน์โหลดฟอนต์ Sarabun-Bold.ttf...", flush=True)
        try:
            r = requests.get(FONT_URL, allow_redirects=True, timeout=15)
            with open(FONT_PATH, 'wb') as f: f.write(r.content)
            print("✅ [INIT] โหลดฟอนต์สำเร็จ!", flush=True)
        except Exception as e:
            print(f"❌ [INIT] โหลดฟอนต์พลาด: {e}", flush=True)
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(FONT_PATH, fontsize)
    except Exception:
        return ImageFont.load_default()

def wrap_and_chunk_thai_text(text, max_chars_per_line=32, max_lines=3):
    try:
        from pythainlp.tokenize import word_tokenize
        words = word_tokenize(text, engine="newmm")
    except ImportError:
        words = list(text)

    chunks, current_chunk, current_line = [], [], ""
    for word in words:
        if len(current_line) + len(word) <= max_chars_per_line:
            current_line += word
        else:
            if current_line: current_chunk.append(current_line)
            current_line = word
            if len(current_chunk) == max_lines:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
    if current_line: current_chunk.append(current_line)
    if current_chunk: chunks.append("\n".join(current_chunk))
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
        
        # 🔻 ขยับ Subtitle ลงมา 50 px (บวกแกน Y เพิ่ม)
        start_y = int(150 * scale_factor) + 50 
        
        rect_padding = int(15 * scale_factor)
        
        draw.rectangle([20 * scale_factor, start_y - rect_padding, width - (20 * scale_factor), start_y + total_height + rect_padding], fill=(0,0,0,160))
        
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
        print(f"❌ [ERROR] สร้างภาพ Subtitle พลาด: {e}", flush=True)
        Image.new('RGBA', (width, height), (0,0,0,0)).save(out_path)

# -----------------------------
# 📊 Create Info Panel
# -----------------------------
def create_info_panel(trade_setup, out_path, width=1080, height=1920):
    print("🎨 [DRAW] กำลังวาดป้าย Info Panel...", flush=True)
    try:
        scale_factor = width / 720.0
        img = Image.new('RGBA', (width, height), (0,0,0,0))
        draw = ImageDraw.Draw(img)

        font_size = int(24 * scale_factor)
        font = get_font(font_size)

        current_price = trade_setup.get('current_price', '-')
        support = trade_setup.get('support', '-')
        resistance = trade_setup.get('resistance', '-')
        target_price = trade_setup.get('target_price', '-')
        trend = trade_setup.get('trend', '-')

        lines = [
            f"ราคาปัจจุบัน  : {current_price}",
            f"แนวรับ         : {support}",
            f"แนวต้าน       : {resistance}",
            f"ราคาเป้าหมาย : {target_price}",
            f"Trend         : {trend}"
        ]

        line_height = font_size + int(15 * scale_factor)
        total_height = len(lines) * line_height
        
        # 🔺 ขยับ Info Panel ขึ้นไป 50 px (ลบแกน Y ออก)
        start_y = height - total_height - int(120 * scale_factor) - 50 
        
        box_x_start = int(40 * scale_factor)
        box_x_end = width - int(40 * scale_factor)

        draw.rectangle(
            [box_x_start, start_y - int(20*scale_factor), box_x_end, start_y + total_height + int(20*scale_factor)], 
            fill=(0,0,0, 180), outline=(255,255,255, 80), width=3
        )

        cur_y = start_y
        for line in lines:
            draw.text((box_x_start + int(30*scale_factor), cur_y), line, font=font, fill="#FFD700")
            cur_y += line_height

        img.save(out_path)
        print("✅ [DRAW] วาดป้าย Info Panel สำเร็จ!", flush=True)
    except Exception as e:
        print(f"❌ [ERROR] Info Panel Error: {e}", flush=True)
        Image.new('RGBA', (width, height), (0,0,0,0)).save(out_path)

# -----------------------------
# 🎬 Video Processing
# -----------------------------
def get_audio_duration(file_path):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)]
        return float(subprocess.run(cmd, stdout=subprocess.PIPE, text=True).stdout.strip())
    except Exception as e:
        print(f"⚠️ [WARNING] คำนวณความยาวเสียงพลาด ใช้ค่าเริ่มต้น 10s: {e}", flush=True)
        return 10.0 

def _run_ffmpeg(cmd: List[str]):
    # เพิ่ม Log พ่นคำสั่ง FFmpeg เต็มๆ ออกมาเพื่อประโยชน์ในการ Debug บน Railway
    print(f"\n⚙️ [FFmpeg EXECUTE]: {' '.join(cmd)}\n", flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0: 
        print(f"❌ [FFmpeg FATAL ERROR]: {proc.stderr}", flush=True)
        raise RuntimeError(f"FFmpeg Error: {proc.stderr}")

async def render_video_task(req: RenderRequest):
    workdir = Path(tempfile.mkdtemp(prefix="render_"))
    has_logo = setup_logo()
    
    print(f"\n" + "="*50, flush=True)
    print(f"🎬 [START] เริ่มงานเรนเดอร์วิดีโอ หุ้น: {req.stock_symbol}", flush=True)
    print(f"📁 [DIR] พื้นที่ทำงานชั่วคราว: {workdir}", flush=True)
    print(f"="*50 + "\n", flush=True)
    
    try:
        assets_dir = workdir / "assets"
        scenes_dir = workdir / "scenes"
        assets_dir.mkdir(parents=True); scenes_dir.mkdir(parents=True)

        scene_mp4s = []
        scenes = sorted(req.data, key=lambda s: s.scene_number)
        last_valid_image = None

        global_info_panel = assets_dir / "info_panel.png"
        create_info_panel(req.trade_setup, str(global_info_panel), DEFAULT_WIDTH, DEFAULT_HEIGHT)

        for s in scenes:
            print(f"\n--- ⏳ [SCENE {s.scene_number}/{len(scenes)}] เริ่มประมวลผล ---", flush=True)
            img_p = assets_dir / f"{s.scene_number}.png"
            aud_p = assets_dir / f"{s.scene_number}.mp3"
            scn_p = scenes_dir / f"{s.scene_number}.mp4"

            try:
                img_data = base64.b64decode(s.image_base64)
                with open(img_p, "wb") as f: f.write(img_data)
                last_valid_image = img_p 
                print(f"🖼️ [SCENE {s.scene_number}] โหลดภาพพื้นหลังสำเร็จ", flush=True)
            except Exception as e:
                print(f"⚠️ [SCENE {s.scene_number}] ภาพมีปัญหา: {e}", flush=True)
                if last_valid_image and last_valid_image.exists(): 
                    shutil.copy(last_valid_image, img_p)
                    print(f"🔄 [SCENE {s.scene_number}] ดึงภาพฉากก่อนหน้ามาใช้แทน", flush=True)
                else:
                    Image.new('RGB', (DEFAULT_WIDTH, DEFAULT_HEIGHT), color='black').save(img_p)
                    last_valid_image = img_p
                    print(f"⬛ [SCENE {s.scene_number}] สร้างภาพสีดำทดแทน", flush=True)

            print(f"🗣️ [SCENE {s.scene_number}] สร้างไฟล์เสียง (TTS)...", flush=True)
            tts = edge_tts.Communicate(s.script, "th-TH-PremwadeeNeural")
            await tts.save(str(aud_p))
            duration = get_audio_duration(aud_p)
            print(f"⏱️ [SCENE {s.scene_number}] ความยาวเสียง: {duration:.2f} วินาที", flush=True)
            
            chunks = wrap_and_chunk_thai_text(s.script, max_chars_per_line=32, max_lines=3)
            total_chars = max(sum(len(c.replace('\n', '')) for c in chunks), 1)
            
            sub_inputs, sub_filters, current_time = [], [], 0.0
            
            for idx, chunk in enumerate(chunks):
                chunk_p = assets_dir / f"{s.scene_number}_sub_{idx}.png"
                create_subtitle_image(chunk, str(chunk_p), DEFAULT_WIDTH, DEFAULT_HEIGHT)
                sub_inputs.extend(["-i", str(chunk_p)])
                
                chunk_duration = (len(chunk.replace('\n', '')) / total_chars) * duration
                start_t, end_t = current_time, current_time + chunk_duration
                current_time = end_t
                
                in_node = "[bg]" if idx == 0 else f"[v{idx}]"
                is_last = (idx == len(chunks) - 1)
                
                out_node = "[final_v]" if (is_last and not has_logo) else ("[final_sub]" if is_last else f"[v{idx+1}]")
                sub_filters.append(f"{in_node}[{3+idx}:v]overlay=0:0:enable='between(t,{start_t:.3f},{end_t:.3f})'{out_node}")

            print(f"🎞️ [SCENE {s.scene_number}] ประกอบร่างวิดีโอ (ซับ {len(chunks)} สไลด์)...", flush=True)
            cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(DEFAULT_FPS), 
                   "-i", str(img_p), "-i", str(aud_p), "-i", str(global_info_panel)] + sub_inputs
            
            fc_parts = [
                f"[0:v]scale={DEFAULT_WIDTH//4}:{DEFAULT_HEIGHT//4}:force_original_aspect_ratio=increase,crop={DEFAULT_WIDTH//4}:{DEFAULT_HEIGHT//4},boxblur=10:5,scale={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}[bg_blur]",
                f"[0:v]scale={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:force_original_aspect_ratio=decrease[fg]",
                f"[bg_blur][fg]overlay=(W-w)/2:(H-h)/2[bg_base]",
                f"[bg_base][2:v]overlay=0:0,fps={DEFAULT_FPS}[bg]"
            ]
            fc_parts.extend(sub_filters)
            
            if has_logo:
                cmd.extend(["-i", LOGO_PATH])
                logo_idx = 3 + len(chunks)
                logo_width = int(200 * (DEFAULT_WIDTH / 720.0))
                fc_parts.append(f"[{logo_idx}:v]format=rgba,scale={logo_width}:-1,colorchannelmixer=aa=0.9[logo]")
                fc_parts.append(f"[final_sub][logo]overlay=W-w-30:30[final_v]")

            cmd.extend([
                "-filter_complex", ";".join(fc_parts),
                "-map", "[final_v]", "-map", "1:a",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "25", 
                "-c:a", "aac", "-b:a", "128k", "-r", str(DEFAULT_FPS), "-t", str(duration), str(scn_p)
            ])
            _run_ffmpeg(cmd)
            scene_mp4s.append(scn_p)
            print(f"✅ [SCENE {s.scene_number}] เรนเดอร์สำเร็จ!", flush=True)

        print(f"\n🔗 [CONCAT] เริ่มรวมไฟล์วิดีโอทั้ง {len(scene_mp4s)} ฉากเข้าด้วยกัน...", flush=True)
        final_name = f"{req.stock_symbol}_{uuid.uuid4().hex[:6]}.mp4"
        final_path = workdir / final_name
        list_p = workdir / "list.txt"
        list_p.write_text("\n".join([f"file '{str(p.absolute())}'" for p in scene_mp4s]))
        _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_p), "-c", "copy", str(final_path)])
        print(f"✅ [CONCAT] วิดีโอรวมเสร็จสมบูรณ์ -> {final_name}", flush=True)

        if storage and GCS_BUCKET:
            print(f"☁️ [UPLOAD] กำลังอัปโหลดขึ้น Google Cloud Storage (Bucket: {GCS_BUCKET})...", flush=True)
            client = storage.Client.from_service_account_info(json.loads(GCP_SA_JSON)) if GCP_SA_JSON else storage.Client()
            client.bucket(GCS_BUCKET).blob(f"{GCS_PREFIX}{final_name}").upload_from_filename(str(final_path))
            print(f"🎉 [SUCCESS] อัปโหลดสำเร็จ! URL: https://storage.googleapis.com/{GCS_BUCKET}/{GCS_PREFIX}{final_name}\n", flush=True)

    except Exception as e:
        print(f"\n❌ [FATAL ERROR]: การเรนเดอร์ล้มเหลว -> {str(e)}\n", flush=True)
    finally:
        print(f"🧹 [CLEANUP] กำลังลบโฟลเดอร์ชั่วคราว {workdir}...", flush=True)
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"✅ [CLEANUP] เคลียร์พื้นที่เรียบร้อย\n", flush=True)

@app.post("/render")
async def create_render_job(req: RenderRequest, background_tasks: BackgroundTasks):
    print(f"\n🚀 [API] ได้รับคำสั่ง Render ใหม่ (หุ้น: {req.stock_symbol}, จำนวนฉาก: {len(req.data)})", flush=True)
    if not req.data: 
        print(f"❌ [API] Error: ไม่พบข้อมูล Scene", flush=True)
        raise HTTPException(status_code=400, detail="No scene data provided")
    background_tasks.add_task(render_video_task, req)
    return {"status": "accepted", "message": "Job added to background queue"}

@app.get("/health")
def health(): return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    print(f"🚀 [SERVER] กำลังเปิดใช้งาน API บนพอร์ต {os.environ.get('PORT', 8080)}...", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))