import os
import uuid
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

APP_VERSION = os.getenv("APP_VERSION", "v2026-02-02-1")
UPLOAD_BUCKET = os.getenv("UPLOAD_BUCKET", "uploads")

app = FastAPI(title="findme-api", version=APP_VERSION)

# Opcional pero recomendable para Orchids (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # luego lo cerramos a tu dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def supabase_admin() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)

class ProcessRequest(BaseModel):
    fingerprint: str
    uploadKey: str | None = None

@app.get("/")
def root():
    return {"ok": True, "service": "findme-api", "version": APP_VERSION}

@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}

@app.get("/__version")
def version():
    return {"version": APP_VERSION}

@app.post("/upload")
async def upload_zip(file: UploadFile = File(...)):
    # Validación rápida
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are allowed")

    sb = supabase_admin()

    # Key donde guardamos el zip
    key = f"zips/{uuid.uuid4()}.zip"

    # Leer bytes del archivo
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    # Subir a Supabase Storage
    # supabase-py v2: storage.from_(bucket).upload(path, file, options?)
    try:
        sb.storage.from_(UPLOAD_BUCKET).upload(
            key,
            data,
            {"content-type": "application/zip"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

    # Para tu UI: ambos iguales hoy (puedes diferenciar luego)
    return {"uploadKey": key, "fingerprint": key}

@app.post("/process")
def process_album(payload: ProcessRequest):
    if not payload.fingerprint:
        raise HTTPException(status_code=400, detail="fingerprint is required")

    sb = supabase_admin()

    # Crear album/job en DB
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
    return {"albumId": album_id, "status": res.data[0]["status"]}

@app.get("/jobs/{album_id}")
def get_job(album_id: str):
    sb = supabase_admin()
    res = (
        sb.table("albums")
        .select("id,status,progress,error_message")
        .eq("id", album_id)
        .single()
        .execute()
    )

    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "albumId": res.data["id"],
        "status": res.data["status"],
        "progress": res.data["progress"],
        "errorMessage": res.data.get("error_message")
    }

