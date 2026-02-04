import os
import io
import zipfile
import mimetypes
from uuid import uuid4
from typing import Optional, List, Dict, Any

import numpy as np
import cv2

from sklearn.cluster import DBSCAN

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

APP_VERSION = "v2026-02-04-2"

app = FastAPI(title="findme-api", version=APP_VERSION)

# -----------------------------
# Config
# -----------------------------
UPLOADS_BUCKET = os.getenv("SUPABASE_BUCKET_UPLOADS", "uploads")

# -----------------------------
# CORS
# -----------------------------
def _parse_allowed_origins() -> List[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if not items:
        items = ["http://localhost:3000", "http://127.0.0.1:3000"]
    return items

ALLOWED_ORIGINS = _parse_allowed_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# -----------------------------
# Supabase
# -----------------------------
def supabase_admin() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing Supabase envs")
    return create_client(url, key)

def _public_uploads_url(path: str) -> str:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    return f"{base}/storage/v1/object/public/{UPLOADS_BUCKET}/{path}"

# -----------------------------
# Face model (lazy)
# -----------------------------
_FACE_APP = None

def _load_face_analyzer():
    from insightface.app import FaceAnalysis
    fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    fa.prepare(ctx_id=0, det_size=(640, 640))
    return fa

def _get_face_app():
    global _FACE_APP
    if _FACE_APP is None:
        _FACE_APP = _load_face_analyzer()
    return _FACE_APP

# -----------------------------
# Models
# -----------------------------
class ProcessRequest(BaseModel):
    fingerprint: str
    uploadKey: Optional[str] = None

# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    return {"ok": True}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/__version")
def version():
    return {
        "version": APP_VERSION,
        "uploadsBucket": UPLOADS_BUCKET,
    }

@app.post("/upload")
async def upload_zip(file: UploadFile = File(...)):
    sb = supabase_admin()

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip supported")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    object_path = f"zips/{uuid4().hex}.zip"

    res = sb.storage.from_(UPLOADS_BUCKET).upload(
        path=object_path,
        file=content,
        file_options={"content-type": "application/zip"},
    )

    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail="Upload failed")

    return {"uploadKey": object_path, "fingerprint": object_path}

@app.post("/process")
def process_album(payload: ProcessRequest, background_tasks: BackgroundTasks):
    sb = supabase_admin()

    if not payload.fingerprint or not payload.uploadKey:
        raise HTTPException(status_code=400, detail="Missing params")

    res = sb.table("albums").insert({
        "fingerprint": payload.fingerprint,
        "status": "pending",
        "progress": 0,
        "photo_count": 0,
        "upload_key": payload.uploadKey
    }).execute()

    if not res.data:
        raise HTTPException(status_code=500, detail="Album insert failed")

    album_id = res.data[0]["id"]
    background_tasks.add_task(_process_zip_album, album_id, payload.uploadKey)

    return {"albumId": album_id}

# ✅ FIX CLAVE: nunca devolver 404 en jobs
@app.get("/jobs/{album_id}")
def get_job(album_id: str):
    sb = supabase_admin()

    res = (
        sb.table("albums")
        .select("id,status,progress,error_message,photo_count")
        .eq("id", album_id)
        .execute()
    )

    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail="Jobs read failed")

    if not res.data:
        # 👇 ESTO evita el error del frontend
        return {
            "albumId": album_id,
            "status": "pending",
            "progress": 0,
            "photoCount": 0,
            "errorMessage": None
        }

    row = res.data[0]
    return {
        "albumId": row["id"],
        "status": row["status"],
        "progress": row["progress"],
        "photoCount": row.get("photo_count", 0),
        "errorMessage": row.get("error_message"),
    }

# -----------------------------
# Helpers
# -----------------------------
def _is_image_filename(name: str) -> bool:
    return name.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))

def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"

# -----------------------------
# Worker
# -----------------------------
def _process_zip_album(album_id: str, upload_key: str):
    sb = supabase_admin()

    try:
        sb.table("albums").update({
            "status": "processing",
            "progress": 5
        }).eq("id", album_id).execute()

        face_app = _get_face_app()

        zip_bytes = sb.storage.from_(UPLOADS_BUCKET).download(upload_key)
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        members = [m for m in zf.namelist() if _is_image_filename(m)]

        inserted = 0
        face_records = []

        for m in members:
            data = zf.read(m)
            ext = os.path.splitext(m)[1].lower()
            path = f"albums/{album_id}/photos/{uuid4().hex}{ext}"

            sb.storage.from_(UPLOADS_BUCKET).upload(
                path=path,
                file=data,
                file_options={"content-type": _guess_mime(m)},
            )

            photo = sb.table("photos").insert({
                "album_id": album_id,
                "storage_path": path
            }).execute()

            if not photo.data:
                continue

            inserted += 1
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue

            for f in face_app.get(img) or []:
                emb = getattr(f, "embedding", None)
                if emb is not None:
                    face_records.append(emb)

        sb.table("albums").update({
            "status": "completed",
            "progress": 100,
            "photo_count": inserted
        }).eq("id", album_id).execute()

    except Exception as e:
        sb.table("albums").update({
            "status": "failed",
            "progress": 0,
            "error_message": str(e)
        }).eq("id", album_id).execute()
