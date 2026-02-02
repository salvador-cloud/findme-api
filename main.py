import os
import io
import zipfile
import mimetypes
from uuid import uuid4
from typing import Optional, List

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

APP_VERSION = "v2026-02-02-3"

app = FastAPI(title="findme-api", version=APP_VERSION)

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
    return f"{base}/storage/v1/object/public/uploads/{path}"

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
    return {"version": APP_VERSION, "allowedOrigins": ALLOWED_ORIGINS}

@app.post("/upload")
async def upload_zip(file: UploadFile = File(...)):
    sb = supabase_admin()

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are supported")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    object_path = f"zips/{uuid4().hex}.zip"

    res = sb.storage.from_("uploads").upload(
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

    if not payload.fingerprint:
        raise HTTPException(status_code=400, detail="fingerprint is required")
    if not payload.uploadKey:
        raise HTTPException(status_code=400, detail="uploadKey is required")

    res = sb.table("albums").insert({
        "fingerprint": payload.fingerprint,
        "status": "pending",
        "progress": 0,
        "photo_count": 0,
        "upload_key": payload.uploadKey
    }).execute()

    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail=f"Failed to create album: {res.error}")
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create album (no data)")

    album_id = res.data[0]["id"]
    background_tasks.add_task(_process_zip_album, album_id, payload.uploadKey)

    return {"albumId": album_id}

@app.get("/jobs/{album_id}")
def get_job(album_id: str):
    sb = supabase_admin()
    res = sb.table("albums").select("id,status,progress,error_message,photo_count").eq("id", album_id).single().execute()

    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail=f"Jobs read failed: {res.error}")
    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "albumId": res.data["id"],
        "status": res.data["status"],
        "progress": res.data["progress"],
        "photoCount": res.data.get("photo_count", 0),
        "errorMessage": res.data.get("error_message")
    }

# -----------------------------
# Background worker helpers
# -----------------------------
def _is_image_filename(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".jpg") or lower.endswith(".jpeg") or lower.endswith(".png") or lower.endswith(".webp")

def _guess_mime(name: str) -> str:
    mt, _ = mimetypes.guess_type(name)
    return mt or "application/octet-stream"

def _get_photo_id(sb: Client, album_id: str, storage_path: str) -> Optional[str]:
    """
    Deterministic lookup. If insert response doesn't return id, we query it back.
    """
    q = (
        sb.table("photos")
        .select("id")
        .eq("album_id", album_id)
        .eq("storage_path", storage_path)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if getattr(q, "error", None):
        return None
    if not q.data:
        return None
    return q.data[0].get("id")

def _get_cluster_id(sb: Client, album_id: str, thumbnail_url: Optional[str]) -> Optional[str]:
    """
    Best-effort lookup. Not perfect, but good enough for MVP placeholder.
    """
    qb = sb.table("face_clusters").select("id").eq("album_id", album_id).order("created_at", desc=True).limit(1)
    q = qb.execute()
    if getattr(q, "error", None):
        return None
    if not q.data:
        return None
    return q.data[0].get("id")

# -----------------------------
# Background worker
# -----------------------------
def _process_zip_album(album_id: str, upload_key: str):
    sb = supabase_admin()

    try:
        sb.table("albums").update({
            "status": "processing",
            "progress": 5,
            "error_message": None
        }).eq("id", album_id).execute()

        zip_bytes = sb.storage.from_("uploads").download(upload_key)
        if not zip_bytes:
            raise RuntimeError("Failed to download ZIP (empty response)")

        sb.table("albums").update({"progress": 10}).eq("id", album_id).execute()

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        members = [m for m in zf.namelist() if m and not m.endswith("/") and _is_image_filename(m)]
        if not members:
            raise RuntimeError("ZIP contains no supported images (.jpg/.png/.webp)")

        total = len(members)
        inserted = 0

        inserted_photos: List[dict] = []  # {photo_id, storage_path, public_url}

        for idx, member in enumerate(members, start=1):
            data = zf.read(member)
            if not data:
                continue

            ext = os.path.splitext(member)[1].lower()
            if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
                continue

            object_path = f"albums/{album_id}/photos/{uuid4().hex}{ext}"
            mime = _guess_mime(member)

            up_res = sb.storage.from_("uploads").upload(
                path=object_path,
                file=data,
                file_options={"content-type": mime},
            )
            if getattr(up_res, "error", None):
                raise RuntimeError(f"Upload image failed: {up_res.error}")

            db_res = sb.table("photos").insert({
                "album_id": album_id,
                "storage_path": object_path
            }).execute()

            if getattr(db_res, "error", None):
                raise RuntimeError(f"Insert photo row failed: {db_res.error}")

            photo_id = None
            if db_res.data and len(db_res.data) > 0:
                photo_id = db_res.data[0].get("id")

            # Fallback: query id if not returned
            if not photo_id:
                photo_id = _get_photo_id(sb, album_id, object_path)

            if photo_id:
                inserted += 1
                try:
                    public_url = _public_uploads_url(object_path)
                except Exception:
                    public_url = None

                inserted_photos.append({
                    "photo_id": photo_id,
                    "storage_path": object_path,
                    "public_url": public_url
                })

            prog = 10 + int((idx / total) * 70)  # 10 -> 80
            sb.table("albums").update({"progress": prog}).eq("id", album_id).execute()

        # -----------------------------------
        # MVP CLUSTERING (placeholder): 1 cluster per photo
        # -----------------------------------
        sb.table("albums").update({"progress": 85}).eq("id", album_id).execute()

        created_links = 0

        for item in inserted_photos:
            pid = item.get("photo_id")
            if not pid:
                continue

            thumb = item.get("public_url")

            cluster_res = sb.table("face_clusters").insert({
                "album_id": album_id,
                "thumbnail_url": thumb
            }).execute()

            if getattr(cluster_res, "error", None):
                raise RuntimeError(f"Insert face_cluster failed: {cluster_res.error}")

            cluster_id = None
            if cluster_res.data and len(cluster_res.data) > 0:
                cluster_id = cluster_res.data[0].get("id")

            if not cluster_id:
                cluster_id = _get_cluster_id(sb, album_id, thumb)

            if not cluster_id:
                raise RuntimeError("Failed to resolve cluster_id after insert")

            link_res = sb.table("photo_faces").insert({
                "photo_id": pid,
                "cluster_id": cluster_id
            }).execute()

            if getattr(link_res, "error", None):
                raise RuntimeError(f"Insert photo_face link failed: {link_res.error}")

            if link_res.data is not None:
                created_links += 1

        sb.table("albums").update({"progress": 95}).eq("id", album_id).execute()

        sb.table("albums").update({
            "status": "completed",
            "progress": 100,
            "photo_count": inserted
        }).eq("id", album_id).execute()

        # Optional: if you want visibility in DB, you could store created_links somewhere later.
        # For now, UI reads clusters + links anyway.

    except Exception as e:
        sb.table("albums").update({
            "status": "failed",
            "progress": 0,
            "error_message": str(e)
        }).eq("id", album_id).execute()
