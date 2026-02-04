import os
import io
import zipfile
import mimetypes
from uuid import uuid4
from typing import Optional, List, Dict, Any

import numpy as np
import cv2

from insightface.app import FaceAnalysis
from sklearn.cluster import DBSCAN

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

APP_VERSION = "v2026-02-04-1"

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
# Face model (singleton)
# -----------------------------
_FACE_APP: Optional[FaceAnalysis] = None

def _get_face_app() -> FaceAnalysis:
    """
    Lazy singleton. CPU-only for Fly small instances.
    """
    global _FACE_APP
    if _FACE_APP is None:
        fa = FaceAnalysis(
            name="buffalo_l",
            providers=["CPUExecutionProvider"],
        )
        # det_size: más grande = mejor para caras chicas, pero más lento.
        fa.prepare(ctx_id=0, det_size=(640, 640))
        _FACE_APP = fa
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

# NEW: clusters list (personas)
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
        raise HTTPException(status_code=500, detail=f"Clusters read failed: {res.error}")

    return {"albumId": album_id, "clusters": res.data or []}

# NEW: photos for a cluster/persona
@app.get("/albums/{album_id}/photos")
def list_photos_for_cluster(album_id: str, clusterId: str = Query(..., alias="clusterId")):
    sb = supabase_admin()

    # 1) get photo_ids from photo_faces
    links = (
        sb.table("photo_faces")
        .select("photo_id")
        .eq("cluster_id", clusterId)
        .execute()
    )

    if getattr(links, "error", None):
        raise HTTPException(status_code=500, detail=f"photo_faces read failed: {links.error}")

    photo_ids = [x["photo_id"] for x in (links.data or []) if x.get("photo_id")]
    if not photo_ids:
        return {"albumId": album_id, "clusterId": clusterId, "photos": []}

    # 2) fetch photos rows
    photos_res = (
        sb.table("photos")
        .select("id,storage_path,created_at")
        .in_("id", photo_ids)
        .order("created_at", desc=False)
        .execute()
    )

    if getattr(photos_res, "error", None):
        raise HTTPException(status_code=500, detail=f"photos read failed: {photos_res.error}")

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

        # vamos a almacenar embeddings para clustering
        face_records: List[Dict[str, Any]] = []  # {photo_id, public_url, emb}

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

            if not photo_id:
                photo_id = _get_photo_id(sb, album_id, object_path)

            if not photo_id:
                # si no podemos resolver el id, seguimos
                continue

            inserted += 1
            public_url = _public_uploads_url(object_path)

            # --- Face detection + embeddings (desde bytes del ZIP) ---
            try:
                nparr = np.frombuffer(data, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if img is not None:
                    face_app = _get_face_app()
                    faces = face_app.get(img)  # todas las caras
                    if faces:
                        for f in faces:
                            emb = getattr(f, "embedding", None)
                            if emb is None:
                                continue
                            emb = emb / (np.linalg.norm(emb) + 1e-12)  # normalize
                            face_records.append({
                                "photo_id": photo_id,
                                "public_url": public_url,
                                "emb": emb.astype(np.float32),
                            })
            except Exception:
                # MVP: no frenamos el job entero por 1 imagen
                pass

            prog = 10 + int((idx / total) * 70)  # 10 -> 80
            sb.table("albums").update({"progress": prog}).eq("id", album_id).execute()

        # -----------------------------------
        # REAL CLUSTERING: clusters = personas
        # -----------------------------------
        sb.table("albums").update({"progress": 85}).eq("id", album_id).execute()

        if not face_records:
            # No hubo caras detectadas. Termina OK pero sin clusters.
            sb.table("albums").update({
                "status": "completed",
                "progress": 100,
                "photo_count": inserted
            }).eq("id", album_id).execute()
            return

        X = np.stack([r["emb"] for r in face_records], axis=0)

        # eps es tu “control knob” principal
        clustering = DBSCAN(eps=0.35, min_samples=1, metric="cosine").fit(X)
        labels = clustering.labels_.astype(int)

        # label -> cluster_id
        label_to_cluster_id: Dict[int, str] = {}

        unique_labels = sorted(set(labels.tolist()))
        for lb in unique_labels:
            # thumbnail: primera foto que aparezca en el cluster
            thumb = None
            for i, r in enumerate(face_records):
                if int(labels[i]) == int(lb):
                    thumb = r.get("public_url")
                    break

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
                # fallback: tomamos el último creado para este álbum
                q = (
                    sb.table("face_clusters")
                    .select("id")
                    .eq("album_id", album_id)
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                if not getattr(q, "error", None) and q.data:
                    cluster_id = q.data[0].get("id")

            if not cluster_id:
                raise RuntimeError("Failed to resolve cluster_id after insert")

            label_to_cluster_id[int(lb)] = cluster_id

        # Link photo_faces (dedupe por (photo_id, cluster_id))
        links = set()
        for i, r in enumerate(face_records):
            pid = r["photo_id"]
            cid = label_to_cluster_id[int(labels[i])]
            links.add((pid, cid))

        payload = [{"photo_id": pid, "cluster_id": cid} for (pid, cid) in links]
        link_res = sb.table("photo_faces").insert(payload).execute()
        if getattr(link_res, "error", None):
            raise RuntimeError(f"Insert photo_faces batch failed: {link_res.error}")

        sb.table("albums").update({"progress": 95}).eq("id", album_id).execute()

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
