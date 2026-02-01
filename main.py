import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

app = FastAPI()


def supabase_admin() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)


class JobCreate(BaseModel):
    uploadKey: str


@app.get("/")
def root():
    return {"ok": True, "service": "findme-api"}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/jobs")
def create_job(payload: JobCreate):
    if not payload.uploadKey:
        raise HTTPException(status_code=400, detail="uploadKey is required")

    sb = supabase_admin()

    # MVP: use uploadKey as fingerprint to enable caching/dedup
    res = sb.table("albums").insert({
        "fingerprint": payload.uploadKey,
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
    res = sb.table("albums").select("id,status,progress,error_message").eq("id", album_id).single().execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "albumId": res.data["id"],
        "status": res.data["status"],
        "progress": res.data["progress"],
        "errorMessage": res.data.get("error_message")
    }

