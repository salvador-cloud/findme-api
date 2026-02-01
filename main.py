import os
import time
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from pydantic import BaseModel
from supabase import create_client, Client

app = FastAPI()


def supabase_admin() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)


class ProcessRequest(BaseModel):
    fingerprint: str
    uploadKey: str


@app.get("/")
def root():
    return {"ok": True, "service": "findme-api"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/__version")
def version():
    return {"version": os.getenv("APP_VERSION", "dev")}


def simulate_processing(album_id: str):
    sb = supabase_admin()

    # Mark as processing
    sb.table("albums").update({"status": "processing", "progress": 5}).eq("id", album_id).execute()

    # Simulate progress updates
    for p in [15, 30, 50, 70, 85, 100]:
        time.sleep(2)
        sb.table("albums").update({"progress": p}).eq("id", album_id).execute()

    # Mark completed
    sb.table("albums").update({"status": "completed", "progress": 100}).eq("id", album_id).execute()


@app.post("/process")
def process_album(payload: ProcessRequest, background_tasks: BackgroundTasks):
    if not payload.uploadKey or not payload.fingerprint:
        raise HTTPException(status_code=400, detail="uploadKey and fingerprint are required")

    sb = supabase_admin()

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

    # Launch background simulation
    background_tasks.add_task(simulate_processing, album_id)

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
