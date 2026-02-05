import os
import mimetypes
from uuid import uuid4
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

APP_VERSION = "v2026-02-04-3"

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
    if not base:
        return None
    return f"{base}/storage/v1/object/public/{UPLOADS_BUCKET}/{path}"

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
        "allowedOrigins": ALLOWED_ORIGINS,
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
        raise HTTPException(status_code=500, detail=f"Upload failed: {res.error}")

    return {"uploadKey": object_path, "fingerprint": object_path}

# ✅ CAMBIO CLAVE: /process SOLO crea el album en DB y devuelve albumId
@app.post("/process")
def process_album(payload: ProcessRequest):
    sb = supabase_admin()

    if not payload.fingerprint or not payload.uploadKey:
        raise HTTPException(status_code=400, detail="Missing params")

    res = sb.table("albums").insert({
        "fingerprint": payload.fingerprint,
        "status": "pending",
        "progress": 0,
        "photo_count": 0,
        "upload_key": payload.uploadKey,
        "error_message": None,
    }).execute()

    if getattr(res, "error", None) or not res.data:
        raise HTTPException(status_code=500, detail="Album insert failed")

    album_id = res.data[0]["id"]
    return {"albumId": album_id}

# ✅ FIX CLAVE: nunca devolver 404 en jobs (para evitar “connection issue” del front)
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
        "progress": row.get("progress", 0),
        "photoCount": row.get("photo_count", 0),
        "errorMessage": row.get("error_message"),
    }

# -----------------------------
# Clusters + Photos (lo llena el worker)
# -----------------------------
@app.get("/albums/{album_id}/clusters")
def list_clusters(album_id: str):
    sb = supabase_admin()

    res = (
        sb.table("face_clusters")
        .select("id,thumbnail_url,created_at")
        .eq("album_id", album_id)
        .order("created_at", desc=False)
        .execute()
    )

    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail="Clusters read failed")

    return {"albumId": album_id, "clusters": res.data or []}

@app.get("/albums/{album_id}/photos")
def list_photos_for_cluster(album_id: str, clusterId: str = Query(..., alias="clusterId")):
    sb = supabase_admin()

    links = (
        sb.table("photo_faces")
        .select("photo_id")
        .eq("cluster_id", clusterId)
        .execute()
    )
    if getattr(links, "error", None):
        raise HTTPException(status_code=500, detail="photo_faces read failed")

    photo_ids = [x["photo_id"] for x in (links.data or []) if x.get("photo_id")]
    if not photo_ids:
        return {"albumId": album_id, "clusterId": clusterId, "photos": []}

    photos_res = (
        sb.table("photos")
        .select("id,storage_path,created_at")
        .in_("id", photo_ids)
        .order("created_at", desc=False)
        .execute()
    )
    if getattr(photos_res, "error", None):
        raise HTTPException(status_code=500, detail="photos read failed")

    photos = []
    for p in (photos_res.data or []):
        sp = p.get("storage_path")
        photos.append({
            "id": p.get("id"),
            "storagePath": sp,
            "url": _public_uploads_url(sp) if sp else None,
            "createdAt": p.get("created_at"),
        })

    return {"albumId": album_id, "clusterId": clusterId, "photos": photos}
