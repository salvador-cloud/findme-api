import os
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

app = FastAPI()

# ---- CORS (clave para que el browser no bloquee y tire "Failed to fetch") ----
allowed_origins = [
    "https://findme.clickcrowdmedia.com",
    "http://localhost:3000",
    "http://localhost:3001",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Supabase Admin ----
def supabase_admin() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)

# ---- Models ----
class ProcessRequest(BaseModel):
    fingerprint: str
    uploadKey: Optional[str] = None

# ---- Basic routes ----
@app.get("/")
def root():
    return {"ok": True, "service": "findme-api"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/__version")
def version():
    return {"version": os.getenv("APP_VERSION", "dev")}

# ---- Upload ZIP (reemplaza /api/upload del front) ----
@app.post("/upload")
async def upload_zip(file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="file is required")

    filename = (file.filename or "").lower()
    if not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are allowed")

    contents = await file.read()
    if not contents or len(contents) < 10:
        raise HTTPException(status_code=400, detail="Empty ZIP file")

    sb = supabase_admin()

    # Guardamos el ZIP en Storage bucket "uploads", carpeta zips/
    key = f"zips/{uuid.uuid4()}.zip"

    # Subida al bucket
    try:
        res = sb.storage.from_("uploads").upload(
            key,
            contents,
            {"content-type": "application/zip"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

    # supabase-py devuelve dict/obj según versión; si falla normalmente lanza excepción.
    # Para MVP devolvemos uploadKey y fingerprint (para dedup/cache)
    return {
        "uploadKey": key,
        "fingerprint": key
    }

# ---- Process (reemplaza /api/process del front) ----
@app.post("/process")
def process_album(payload: ProcessRequest):
    if not payload.fingerprint:
        raise HTTPException(status_code=400, detail="fingerprint is required")

    sb = supabase_admin()

    # Creamos el album (job)
    res = sb.table("albums").insert({
        "fingerprint": payload.fingerprint,
        "status": "pending",
        "progress": 0,
        "photo_count": 0,
        "upload_key": payload.uploadKey
    }).execute()

    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create album")

    album_id = res.data[0]["id"]
    return {"albumId": album_id}

# ---- Jobs (tu API de estado) ----
@app.get("/jobs/{album_id}")
def get_job(album_id: str):
    sb = supabase_admin()
    res = sb.table("albums").select("id,status,progress,error_message").eq("id", album_id).single().execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "albumId": res.data["id"],
        "status": res.data["status"],
        "progress": res.data["progress"],
        "errorMessage": res.data.get("error_message")
    }
