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

APP_VERSION = "v2026-02-04-1"

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
# Supabase Admin
# -----------------------------
def supabase_admin() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)

def _public_uploads_url(path: str) -> str:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("Missing SUPABASE_URL")
    return f"{base}/storage/v1/object/public/{UPLOADS_BUCKET}/{path}"

# -----------------------------
# Face model (singleton, lazy)
# -----------------------------
_FACE_APP = None  # type: ignore

def _load_face_analyzer():
    from insightface.app import FaceAnalysis
    fa = FaceAnalysis(
        name="buffalo_l",
        providers=["CPUExecutionProvider"],
    )
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
    return {"ok": True, "service": "findme-api"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/__version")
def version():
    return {
        "version": APP_VERSION,
        "allowedOrigins": ALLOWED_ORIGINS,
        "uploadsBucket": UPLOADS_BUCKET,
    }

@app.post("/upload")
async def upload_zip(file: UploadFile = File(...)):
    sb = supabase_admin()

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are supported")

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
        raise HTTPException(status_code=500, detail=f"Upload failed: {res.error}")

    return {"uploadKey": object_path, "fingerprint": object_path}

@app.post("/process")
def process_album(payload: ProcessRequest, background_tasks: BackgroundTasks):
    sb = supabase_admin()

    if not payload.fingerprint or not payload.uploadKey:
        raise HTTPException(status_code=400, detail="fingerprint and uploadKey are required")

    res = sb.table("albums").insert({
        "fingerprint": payload.fingerprint,
        "status": "pending",
        "progress": 0,
        "photo_count": 0,
        "upload_key": payload.uploadKey
    }).execute()

    if getattr(res, "error", None) or not res.data:
        raise HTTPException(status_code=500, detail="Failed to create album")

    album_id = res.data[0]["id"]
    background_tasks.add_task(_process_zip_album, album_id, payload.uploadKey)

    return {"albumId": album_id}

# -----------------------------
# Helpers
# -----------------------------
def _is_image_filename(name: str) -> bool:
    return name.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))

def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"

# -----------------------------
# Background worker
# -----------------------------
def _process_zip_album(album_id: str, upload_key: str):
    sb = supabase_admin()
    try:
        sb.table("albums").update({"status": "processing", "progress": 5}).eq("id", album_id).execute()

        face_app = _get_face_app()

        zip_bytes = sb.storage.from_(UPLOADS_BUCKET).download(upload_key)
        if not zip_bytes:
            raise RuntimeError("Failed to download ZIP")

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        members = [m for m in zf.namelist() if _is_image_filename(m)]
        if not members:
            raise RuntimeError("ZIP contains no supported images")

        inserted = 0
        face_records: List[Dict[str, Any]] = []

        for idx, member in enumerate(members, start=1):
            data = zf.read(member)
            ext = os.path.splitext(member)[1].lower()
            object_path = f"albums/{album_id}/photos/{uuid4().hex}{ext}"

            sb.storage.from_(UPLOADS_BUCKET).upload(
                path=object_path,
                file=data,
                file_options={"content-type": _guess_mime(member)},
            )

            photo = sb.table("photos").insert({
                "album_id": album_id,
                "storage_path": object_path
            }).execute()

            if not photo.data:
                continue

            photo_id = photo.data[0]["id"]
            inserted += 1

            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue

            for f in face_app.get(img) or []:
                emb = getattr(f, "embedding", None)
                if emb is not None:
                    emb = emb / (np.linalg.norm(emb) + 1e-12)
                    face_records.append({
                        "photo_id": photo_id,
                        "emb": emb.astype(np.float32),
                    })

        if not face_records:
            sb.table("albums").update({
                "status": "completed",
                "progress": 100,
                "photo_count": inserted
            }).eq("id", album_id).execute()
            return

        X = np.stack([r["emb"] for r in face_records])
        labels = DBSCAN(eps=0.35, min_samples=1, metric="cosine").fit(X).labels_

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
