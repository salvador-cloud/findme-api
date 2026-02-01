import os
import time
from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
from supabase import create_client, Client

APP_VERSION = "v2026-02-02-1"

app = FastAPI(title="findme-api", version=APP_VERSION)


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
    return {"ok": True, "service": "findme-api"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/__version")
def version():
    return {"version": APP_VERSION}


@app.post("/upload")
async def upload_zip(file: UploadFile = File(...)):
    """
    Upload ZIP to Supabase Storage bucket: uploads
    Stored path: zips/<uuid>.zip
    Returns { uploadKey, fingerprint }
    """
    sb = supabase_admin()

    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are supported")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    object_path = f"zips/{os.urandom(16).hex()}.zip"

    # Upload to bucket "uploads"
    res = sb.storage.from_("uploads").upload(
        path=object_path,
        file=content,
        file_options={"content-type": "application/zip"},
    )

    # If upload fails, supabase-py returns an error-like object
    # We'll be defensive:
    if getattr(res, "error", None):
        raise HTTPException(status_code=500, detail=f"Upload failed: {res.error}")

    return {
        "uploadKey": object_path,
        "fingerprint": object_path
    }


@app.post("/process")
def process_album(payload: ProcessRequest):
    """
    Creates album job record in DB.
    For demo: auto-completes the job after creation.
    """
    sb = supabase_admin()

    if not payload.fingerprint:
        raise HTTPException(status_code=400, detail="fingerprint is required")

    # Create album row
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

    # ✅ DEMO ONLY: simulate some work, then mark completed
    time.sleep(1)

    sb.table("albums").update({
        "status": "completed",
        "progress": 100
    }).eq("id", album_id).execute()

    return {"albumId": album_id}


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
