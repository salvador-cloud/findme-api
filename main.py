import os
import io
import zipfile
import requests
import hashlib
import secrets
import redis
from datetime import datetime, timezone
from uuid import uuid4
from typing import Optional, List, Dict, Any, Tuple

from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from supabase import create_client, Client

APP_VERSION = "v2026-02-08-p3-redis-list-queue"

app = FastAPI(title="findme-api", version=APP_VERSION)

# -----------------------------
# Config
# -----------------------------
UPLOADS_BUCKET = os.getenv("SUPABASE_BUCKET_UPLOADS", "uploads")

# P0 guards (API-side)
MAX_ZIP_MB = int(os.getenv("MAX_ZIP_MB", "50"))
MAX_PHOTOS_PER_ALBUM = int(os.getenv("MAX_PHOTOS_PER_ALBUM", "500"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))

# Pagination (P2.2)
MAX_PAGE_LIMIT = int(os.getenv("MAX_PAGE_LIMIT", "200"))
DEFAULT_CLUSTERS_LIMIT = int(os.getenv("DEFAULT_CLUSTERS_LIMIT", "50"))
DEFAULT_PHOTOS_LIMIT = int(os.getenv("DEFAULT_PHOTOS_LIMIT", "60"))

# Recovery code (anti-abuse)
ALBUM_CODE_SALT = os.getenv("ALBUM_CODE_SALT", "")  # REQUIRED in Fly secrets for API

# Redis queue (P3 - ultra light)
REDIS_URL = os.getenv("REDIS_URL", "").strip()
RQ_QUEUE_NAME = os.getenv("RQ_QUEUE_NAME", "findme").strip()


def _enqueue_job(job_id: str) -> None:
    """
    Ultra-light enqueue:
    - Push job_id string to Redis list: rq:queue:<name>
    Best-effort: if Redis down/misconfigured, DO NOT break API flow.
    """
    if not REDIS_URL or not job_id:
        return

    try:
        r = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=10,
            health_check_interval=30,
        )
        r.rpush(f"rq:queue:{RQ_QUEUE_NAME}", job_id)
    except Exception:
        return


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


def _public_uploads_url(path: str) -> Optional[str]:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not base or not path:
        return None
    return f"{base}/storage/v1/object/public/{UPLOADS_BUCKET}/{path.lstrip('/')}"


# -----------------------------
# Paging helpers (P2.2)
# -----------------------------
def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _parse_paging(
    limit: Optional[int],
    offset: Optional[int],
    default_limit: int,
) -> Tuple[int, int]:
    l = default_limit if limit is None else int(limit)
    o = 0 if offset is None else int(offset)

    l = _clamp_int(l, 1, MAX_PAGE_LIMIT)
    o = max(0, o)
    return l, o


def _apply_range_q(q, offset: int, limit: int):
    """
    Supabase postgrest range is inclusive.
    We use +1 strategy (fetch limit+1) for hasMore without COUNT().
    """
    start = offset
    end = offset + limit - 1
    return q.range(start, end)


# -----------------------------
# Recovery code helpers
# -----------------------------
def _require_code_salt() -> None:
    if not ALBUM_CODE_SALT:
        raise RuntimeError("Missing ALBUM_CODE_SALT env (set as Fly secret on API app)")


def _generate_recovery_code(length: int = 12) -> str:
    """
    Human-friendly code (no 0/O/1/I confusion).
    Example: K7M9-2XPD-RQ5A
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(length))
    if length >= 12:
        return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"
    if length >= 8:
        return f"{raw[0:4]}-{raw[4:8]}"
    return raw


def _normalize_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "").replace("_", "-")


def _hash_code(code: str) -> str:
    _require_code_salt()
    c = _normalize_code(code)
    data = (ALBUM_CODE_SALT + ":" + c).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _hint_from_code(code: str) -> str:
    c = _normalize_code(code)
    clean = c.replace("-", "")
    return clean[-4:] if len(clean) >= 4 else clean


def _get_album_code_or_403(x_album_code: Optional[str], code_qs: Optional[str]) -> str:
    code = x_album_code or code_qs
    if not code:
        raise HTTPException(status_code=403, detail="Missing album code")
    return code


def _require_album_access(
    sb: Client,
    album_id: str,
    x_album_code: Optional[str],
    code_qs: Optional[str],
) -> None:
    """
    Enforces recovery code if album has access_code_hash set.
    Backward compatible: if column is null/empty, allow.
    """
    res = (
        sb.table("albums")
        .select("id,access_code_hash,access_code_hint")
        .eq("id", album_id)
        .limit(1)
        .execute()
    )
    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail="albums read failed")
    if not res.data:
        raise HTTPException(status_code=404, detail="Album not found")

    row = res.data[0]
    stored_hash = (row.get("access_code_hash") or "").strip()
    if not stored_hash:
        return

    provided = _get_album_code_or_403(x_album_code, code_qs)
    try:
        provided_hash = _hash_code(provided)
    except RuntimeError:
        raise HTTPException(status_code=500, detail="Server misconfigured (missing ALBUM_CODE_SALT)")

    if provided_hash != stored_hash:
        hint = row.get("access_code_hint")
        if hint:
            raise HTTPException(status_code=403, detail=f"Invalid album code (hint: ****{hint})")
        raise HTTPException(status_code=403, detail="Invalid album code")


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
        "limits": {
            "maxZipMB": MAX_ZIP_MB,
            "maxPhotosPerAlbum": MAX_PHOTOS_PER_ALBUM,
            "maxPageLimit": MAX_PAGE_LIMIT,
            "defaultClustersLimit": DEFAULT_CLUSTERS_LIMIT,
            "defaultPhotosLimit": DEFAULT_PHOTOS_LIMIT,
        },
        "recoveryCode": {
            "enabled": True,
            "requiresSalt": True,
            "saltConfigured": bool(ALBUM_CODE_SALT),
        },
        "queue": {
            "enabled": bool(REDIS_URL),
            "provider": "redis_list",
            "queueName": RQ_QUEUE_NAME,
            "redisKey": f"rq:queue:{RQ_QUEUE_NAME}",
        },
    }


# -----------------------------
# ZIP helpers (API-side validation)
# -----------------------------
def _count_images_in_zip(zip_bytes: bytes) -> int:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ZIP file")

    count = 0
    for name in zf.namelist():
        n = name.lower()
        if n.endswith("/") or not (n.endswith(".jpg") or n.endswith(".jpeg") or n.endswith(".png")):
            continue
        count += 1
        if count > MAX_PHOTOS_PER_ALBUM:
            break
    return count


# -----------------------------
# Upload ZIP
# -----------------------------
@app.post("/upload")
async def upload_zip(file: UploadFile = File(...)):
    sb = supabase_admin()

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip supported")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    max_bytes = MAX_ZIP_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"ZIP too large. Max {MAX_ZIP_MB}MB")

    img_count = _count_images_in_zip(content)
    if img_count == 0:
        raise HTTPException(status_code=400, detail="ZIP contains no supported images (.jpg/.jpeg/.png)")
    if img_count > MAX_PHOTOS_PER_ALBUM:
        raise HTTPException(status_code=413, detail=f"Too many photos in ZIP. Max {MAX_PHOTOS_PER_ALBUM}")

    object_path = f"zips/{uuid4().hex}.zip"

    res = sb.storage.from_(UPLOADS_BUCKET).upload(
        path=object_path,
        file=content,
        file_options={"content-type": "application/zip"},
    )

    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail=f"Upload failed: {res.error}")

    return {"uploadKey": object_path, "fingerprint": object_path}


# -----------------------------
# PROCESS
# -----------------------------
@app.post("/process")
def process_album(payload: ProcessRequest):
    sb = supabase_admin()

    if not payload.fingerprint or not payload.uploadKey:
        raise HTTPException(status_code=400, detail="Missing params")

    fingerprint = payload.fingerprint.strip()
    upload_key = payload.uploadKey.strip()

    existing = (
        sb.table("albums")
        .select("id,status,access_code_hash")
        .eq("fingerprint", fingerprint)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    album_id = None
    reused = False

    if not getattr(existing, "error", None) and existing.data:
        row = existing.data[0]
        if row.get("status") in ("queued", "processing"):
            album_id = row["id"]
            reused = True

    recovery_code: Optional[str] = None

    if not album_id:
        try:
            _require_code_salt()
            recovery_code = _generate_recovery_code(12)
            code_hash = _hash_code(recovery_code)
            code_hint = _hint_from_code(recovery_code)
            code_created_at = datetime.now(timezone.utc).isoformat()
        except Exception:
            recovery_code = None
            code_hash = None
            code_hint = None
            code_created_at = None

        insert_payload: Dict[str, Any] = {
            "fingerprint": fingerprint,
            "status": "queued",
            "progress": 0,
            "photo_count": 0,
            "upload_key": upload_key,
            "error_message": None,
        }
        if code_hash:
            insert_payload["access_code_hash"] = code_hash
            insert_payload["access_code_hint"] = code_hint
            insert_payload["access_code_created_at"] = code_created_at

        res = sb.table("albums").insert(insert_payload).execute()

        if getattr(res, "error", None) or not res.data:
            raise HTTPException(status_code=500, detail="Album insert failed")

        album_id = res.data[0]["id"]

    job_check = (
        sb.table("jobs")
        .select("id,status")
        .eq("album_id", album_id)
        .in_("status", ["pending", "processing"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if getattr(job_check, "error", None):
        raise HTTPException(status_code=500, detail="Jobs read failed")

    if job_check.data:
        job_id = job_check.data[0]["id"]
        resp: Dict[str, Any] = {"albumId": album_id, "jobId": job_id}
        if recovery_code and not reused:
            resp["recoveryCode"] = recovery_code
        return resp

    job_id = str(uuid4())

    job_res = sb.table("jobs").insert(
        {
            "id": job_id,
            "status": "pending",
            "album_id": album_id,
            "zip_path": upload_key,
            "error": None,
            "result": None,
        }
    ).execute()

    if getattr(job_res, "error", None):
        raise HTTPException(status_code=500, detail="Job insert failed")

    # enqueue (best-effort)
    _enqueue_job(job_id)

    resp: Dict[str, Any] = {"albumId": album_id, "jobId": job_id}
    if recovery_code and not reused:
        resp["recoveryCode"] = recovery_code
    return resp


# -----------------------------
# JOB STATUS (no auth for polling)
# -----------------------------
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
            "status": "queued",
            "progress": 0,
            "photoCount": 0,
            "errorMessage": None,
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
# CLUSTERS (protected + paginated)
# -----------------------------
@app.get("/albums/{album_id}/clusters")
def list_clusters(
    album_id: str,
    limit: Optional[int] = Query(default=None, ge=1, alias="limit"),
    offset: Optional[int] = Query(default=None, ge=0, alias="offset"),
    x_album_code: Optional[str] = Header(default=None, alias="X-Album-Code"),
    code: Optional[str] = Query(default=None, alias="code"),
):
    sb = supabase_admin()
    _require_album_access(sb, album_id, x_album_code, code)

    page_limit, page_offset = _parse_paging(limit, offset, DEFAULT_CLUSTERS_LIMIT)
    fetch_n = page_limit + 1

    q = (
        sb.table("face_clusters")
        .select("id,thumbnail_url,created_at")
        .eq("album_id", album_id)
        .order("created_at", desc=False)
    )
    res = _apply_range_q(q, page_offset, fetch_n).execute()

    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail="Clusters read failed")

    rows = res.data or []
    has_more = len(rows) > page_limit
    clusters = rows[:page_limit] if has_more else rows

    return {
        "albumId": album_id,
        "clusters": clusters,
        "paging": {
            "limit": page_limit,
            "offset": page_offset,
            "returned": len(clusters),
            "hasMore": has_more,
            "nextOffset": (page_offset + page_limit) if has_more else None,
        },
    }


# -----------------------------
# PHOTOS POR CLUSTER (protected + paginated)
# -----------------------------
@app.get("/albums/{album_id}/photos")
def list_photos_for_cluster(
    album_id: str,
    clusterId: str = Query(..., alias="clusterId"),
    limit: Optional[int] = Query(default=None, ge=1, alias="limit"),
    offset: Optional[int] = Query(default=None, ge=0, alias="offset"),
    x_album_code: Optional[str] = Header(default=None, alias="X-Album-Code"),
    code: Optional[str] = Query(default=None, alias="code"),
):
    sb = supabase_admin()
    _require_album_access(sb, album_id, x_album_code, code)

    page_limit, page_offset = _parse_paging(limit, offset, DEFAULT_PHOTOS_LIMIT)
    fetch_n = page_limit + 1

    links_q = (
        sb.table("photo_faces")
        .select("photo_id")
        .eq("cluster_id", clusterId)
        .order("photo_id", desc=False)
    )
    links_res = _apply_range_q(links_q, page_offset, fetch_n).execute()

    if getattr(links_res, "error", None):
        raise HTTPException(status_code=500, detail="photo_faces read failed")

    link_rows = links_res.data or []
    has_more = len(link_rows) > page_limit
    link_rows = link_rows[:page_limit] if has_more else link_rows

    photo_ids = [x.get("photo_id") for x in link_rows if x.get("photo_id")]
    if not photo_ids:
        return {
            "albumId": album_id,
            "clusterId": clusterId,
            "photos": [],
            "paging": {
                "limit": page_limit,
                "offset": page_offset,
                "returned": 0,
                "hasMore": False,
                "nextOffset": None,
            },
        }

    photos_res = (
        sb.table("photos")
        .select("id,storage_path,created_at")
        .in_("id", photo_ids)
        .execute()
    )
    if getattr(photos_res, "error", None):
        raise HTTPException(status_code=500, detail="photos read failed")

    by_id: Dict[str, Dict[str, Any]] = {str(p.get("id")): p for p in (photos_res.data or []) if p.get("id")}
    ordered = []
    for pid in photo_ids:
        p = by_id.get(str(pid))
        if not p:
            continue
        sp = p.get("storage_path")
        ordered.append(
            {
                "id": p.get("id"),
                "storagePath": sp,
                "url": _public_uploads_url(sp) if sp else None,
                "createdAt": p.get("created_at"),
            }
        )

    return {
        "albumId": album_id,
        "clusterId": clusterId,
        "photos": ordered,
        "paging": {
            "limit": page_limit,
            "offset": page_offset,
            "returned": len(ordered),
            "hasMore": has_more,
            "nextOffset": (page_offset + page_limit) if has_more else None,
        },
    }


# -----------------------------
# DOWNLOAD ZIP POR CLUSTER (protected)
# -----------------------------
@app.get("/albums/{album_id}/download")
def download_cluster(
    album_id: str,
    clusterId: str = Query(..., alias="clusterId"),
    x_album_code: Optional[str] = Header(default=None, alias="X-Album-Code"),
    code: Optional[str] = Query(default=None, alias="code"),
):
    sb = supabase_admin()
    _require_album_access(sb, album_id, x_album_code, code)

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
        raise HTTPException(status_code=404, detail="No photos for cluster")

    photos_res = (
        sb.table("photos")
        .select("id,storage_path")
        .in_("id", photo_ids)
        .execute()
    )
    if getattr(photos_res, "error", None):
        raise HTTPException(status_code=500, detail="photos read failed")

    items = photos_res.data or []
    if not items:
        raise HTTPException(status_code=404, detail="No photos found")

    if len(items) > MAX_PHOTOS_PER_ALBUM:
        raise HTTPException(status_code=413, detail="Too many photos to download")

    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not base:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_URL")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in items:
            sp = p.get("storage_path")
            if not sp:
                continue
            url = f"{base}/storage/v1/object/public/{UPLOADS_BUCKET}/{sp.lstrip('/')}"
            r = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            if r.status_code != 200:
                continue

            ext = os.path.splitext(sp)[1] or ".jpg"
            zf.writestr(f"{p.get('id')}{ext}", r.content)

    buf.seek(0)
    data = buf.read()
    if not data:
        raise HTTPException(status_code=404, detail="Could not build ZIP")

    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="findme_{album_id}_{clusterId}.zip"'},
    )


# -----------------------------
# DELETE ALBUM (protected)
# -----------------------------
@app.delete("/albums/{album_id}")
def delete_album(
    album_id: str,
    x_album_code: Optional[str] = Header(default=None, alias="X-Album-Code"),
    code: Optional[str] = Query(default=None, alias="code"),
):
    sb = supabase_admin()
    _require_album_access(sb, album_id, x_album_code, code)

    photos = (
        sb.table("photos")
        .select("id,storage_path")
        .eq("album_id", album_id)
        .execute()
    )
    if getattr(photos, "error", None):
        raise HTTPException(status_code=500, detail="photos read failed")

    photo_rows = photos.data or []
    photo_paths = [r.get("storage_path") for r in photo_rows if r.get("storage_path")]
    photo_ids = [r.get("id") for r in photo_rows if r.get("id")]

    faces = (
        sb.table("face_embeddings")
        .select("id")
        .eq("album_id", album_id)
        .execute()
    )
    if getattr(faces, "error", None):
        raise HTTPException(status_code=500, detail="face_embeddings read failed")

    face_rows = faces.data or []
    face_thumb_paths = [f"albums/{album_id}/faces/{r['id']}.jpg" for r in face_rows if r.get("id")]

    alb = sb.table("albums").select("upload_key").eq("id", album_id).execute()
    zip_paths = []
    if not getattr(alb, "error", None) and alb.data and alb.data[0].get("upload_key"):
        zip_paths = [alb.data[0]["upload_key"]]

    try:
        if photo_paths:
            sb.storage.from_(UPLOADS_BUCKET).remove(photo_paths)
        if face_thumb_paths:
            sb.storage.from_(UPLOADS_BUCKET).remove(face_thumb_paths)
        if zip_paths:
            sb.storage.from_(UPLOADS_BUCKET).remove(zip_paths)
    except Exception:
        pass

    try:
        for pid in photo_ids:
            sb.table("photo_faces").delete().eq("photo_id", pid).execute()

        sb.table("face_embeddings").delete().eq("album_id", album_id).execute()
        sb.table("face_clusters").delete().eq("album_id", album_id).execute()
        sb.table("photos").delete().eq("album_id", album_id).execute()
        sb.table("jobs").delete().eq("album_id", album_id).execute()
        sb.table("albums").delete().eq("id", album_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB cleanup failed: {str(e)}")

    return {
        "ok": True,
        "albumId": album_id,
        "deleted": {
            "photos": len(photo_ids),
            "photoPaths": len(photo_paths),
            "faceThumbs": len(face_thumb_paths),
            "zips": len(zip_paths),
        },
    }
