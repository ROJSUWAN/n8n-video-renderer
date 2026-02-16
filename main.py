import os, uuid, json, subprocess, tempfile, shutil
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google.cloud import storage

app = FastAPI()

# ===== Models =====
class Scene(BaseModel):
    scene_order: int
    duration_sec: int
    pixabay_video_url: str
    tts_audio_url: str
    script: Optional[str] = None

class RenderRequest(BaseModel):
    project_id: str
    language: str = "th-TH"
    style: str = "A"
    scenes: List[Scene]
    # output config
    out_bucket: str
    out_prefix: str = "renders"
    aspect: str = "16:9"   # "16:9" or "9:16"
    resolution: str = "1920x1080"  # for 9:16 use 1080x1920

def run(cmd: List[str]):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"CMD failed: {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p.stdout

def download(url: str, out_path: str):
    # curl is usually present; if not, switch to python requests later
    run(["bash", "-lc", f"curl -L --fail --retry 3 --retry-delay 1 -o {shlex(out_path)} {shlex(url)}"])

def shlex(path: str) -> str:
    return "'" + path.replace("'", "'\"'\"'") + "'"

def upload_gcs(local_path: str, bucket: str, object_name: str) -> str:
    client = storage.Client()
    b = client.bucket(bucket)
    blob = b.blob(object_name)
    blob.upload_from_filename(local_path, content_type="video/mp4")
    # direct URL (works if object is public OR you later switch to signed URLs)
    return f"https://storage.googleapis.com/{bucket}/{object_name}"

@app.post("/render")
def render(req: RenderRequest):
    if not req.scenes:
        raise HTTPException(400, "scenes is empty")

    # sort scenes
    scenes = sorted(req.scenes, key=lambda s: s.scene_order)

    # choose resolution
    if req.aspect == "9:16":
        target_res = req.resolution or "1080x1920"
    else:
        target_res = req.resolution or "1920x1080"

    job_id = f"{req.project_id}_{uuid.uuid4().hex[:8]}"

    workdir = tempfile.mkdtemp(prefix="render_")
    try:
        parts = []
        for s in scenes:
            v_in = os.path.join(workdir, f"v_{s.scene_order:03d}.mp4")
            a_in = os.path.join(workdir, f"a_{s.scene_order:03d}.mp3")

            # download video + audio
            run(["bash","-lc", f"curl -L --fail --retry 3 -o {shlex(v_in)} {shlex(s.pixabay_video_url)}"])
            run(["bash","-lc", f"curl -L --fail --retry 3 -o {shlex(a_in)} {shlex(s.tts_audio_url)}"])

            out_part = os.path.join(workdir, f"part_{s.scene_order:03d}.mp4")

            # scale/pad video to target, trim both to duration, then mux
            # -shortest ensures it stops when either ends (we also trim explicitly)
            vf = (
                f"scale={target_res}:force_original_aspect_ratio=decrease,"
                f"pad={target_res}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
            )

            cmd = [
                "ffmpeg","-y",
                "-i", v_in,
                "-i", a_in,
                "-t", str(s.duration_sec),
                "-vf", vf,
                "-c:v","libx264","-preset","veryfast","-crf","22",
                "-c:a","aac","-b:a","128k",
                "-shortest",
                out_part
            ]
            run(cmd)
            parts.append(out_part)

        # concat parts
        concat_list = os.path.join(workdir, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for p in parts:
                f.write(f"file '{p.replace(\"'\", \"'\\\\''\")}'\n")

        final_mp4 = os.path.join(workdir, f"{job_id}.mp4")
        run([
            "ffmpeg","-y",
            "-f","concat","-safe","0",
            "-i", concat_list,
            "-c","copy",
            final_mp4
        ])

        # upload to GCS
        object_name = f"{req.out_prefix}/{req.project_id}/{job_id}.mp4"
        url = upload_gcs(final_mp4, req.out_bucket, object_name)

        return {
            "job_id": job_id,
            "output_gcs_object": object_name,
            "output_url": url
        }

    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

@app.get("/health")
def health():
    return {"ok": True}