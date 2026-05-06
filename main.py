import os
import json
import pandas as pd
import shutil
import time
import asyncio
import bcrypt
from datetime import datetime, timedelta
from typing import List

import uvicorn
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, status, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse # Dosya gönderimi için gerekli

from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship

from passlib.context import CryptContext
from jose import JWTError, jwt
import fitz  # PyMuPDF 
from google import genai 

# --- 1. AYARLAR ---
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("JWT_ALGORITHM")
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL") + "?sslmode=require"

# Bcrypt yaması
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type('About', (), {'__version__': bcrypt.__version__})

engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

try:
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except Exception as e:
    print(f"Bcrypt başlatılamadı: {e}")

# --- 2. VERİ MODELLERİ ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    analyses = relationship("Analysis", backref="user", cascade="all, delete-orphan")

class Analysis(Base):
    __tablename__ = "analyses"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    file_name = Column(String)
    is_saved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# --- 3. YARDIMCI FONKSİYONLAR ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_access_token(data: dict):
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

# --- 4. FASTAPI UYGULAMASI ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# Statik dosyalara direkt erişim (Güvenlik istemeyen durumlar için)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- 5. ENDPOINTLER ---

@app.get("/")
def home():
    return {"api_name": "Hemithea Analytics Engine", "status": "active"}

# GÜVENLİ DOSYA ÇEKME (STREAMLIT İÇİN)
@app.get("/get-analysis/{username}/{filename}")
async def get_analysis_file(username: str, filename: str, token: str):
    try:
        # Token doğrulaması
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim!")

    file_path = os.path.join(UPLOAD_DIR, username, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı.")
    
    return FileResponse(file_path)

# ... (Buraya Register ve Login fonksiyonlarını ekleyebilirsin) ...

@app.post("/upload-csv")
async def upload_file(
    token: str,
    username: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    try:
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        raise HTTPException(status_code=401, detail="Geçersiz anahtar!")

    user_folder = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    file_name = "network_data.csv"
    file_path = os.path.join(user_folder, file_name)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    return {"status": "success", "file_url": f"/get-analysis/{username}/{file_name}"}

# PDF Analiz Endpointi (İçindeki Gemini döngüsü senin mevcut kodundaki gibi kalmalı)
@app.post("/upload-pdf")
async def process_pdf_analysis(
    token: str,
    username: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    # Token ve kullanıcı kontrolü kısımları aynı...
    # Dosyayı "hna_data.csv" olarak kaydettiğinden emin ol.
    pass
