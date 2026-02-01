import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

app = FastAPI()

# CORS: allow Orchids + tu dominio final
allowed_origins = [
    "https://finde.clickcrowdmedia.com",
    "https://*.orchids.ai",  # si Orchids usa subdominios
    "https://orchids.ai",
    "http://localhost:3000",
]

# Si Orchids usa dominios variables y no te deja wildcard exacto,
# poné allow_origin_regex (más robusto)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https://.*\.orchids\.ai",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
