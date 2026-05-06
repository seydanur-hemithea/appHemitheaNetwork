
import os
import json
import pandas as pd
import shutil
import asyncio
import bcrypt
from datetime import datetime
from typing import List

import uvicorn
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship

from passlib.context import CryptContext
from jose import JWTError, jwt
import fitz  # PyMuPDF
from google import genai
from google.genai import types

# --- 1. KONFİGÜRASYON VE VERİTABANI ---
SECRET_KEY = os.getenv("SECRET_KEY", "Hemithea_Super_Secret_Key_2024")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

if SQLALCHEMY_DATABASE_URL and "sslmode" not in SQLALCHEMY_DATABASE_URL:
    SQLALCHEMY_DATABASE_URL += "?sslmode=require"

engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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
    analysis_type = Column(String, nullable=True) # PDF mi CSV mi
    created_at = Column(DateTime, default=datetime.utcnow)

# VERİTABANI GÜNCELLEME (Migration): Sütun yoksa ekle
Base.metadata.create_all(bind=engine)
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS analysis_type VARCHAR"))
        conn.commit()
    except Exception as e:
        print(f"Tablo güncelleme uyarısı: {e}")

# --- 3. YARDIMCI FONKSİYONLAR ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_access_token(data: dict):
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        raise HTTPException(status_code=401, detail="Geçersiz anahtar.")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = "gemini-2.0-flash"

# --- 4. FASTAPI UYGULAMASI ---
app = FastAPI(title="Hemithea Analytics API", version="2.6.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")

# --- 5. ENDPOINTLER ---

@app.get("/")
def read_root():
    return {"message": "Hemithea Engine Online", "status": "active"}

@app.post("/register")
def register(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Kullanıcı mevcut")
    new_user = User(username=username, hashed_password=pwd_context.hash(password))
    db.add(new_user)
    db.commit()
    return {"status": "success"}

@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Hatalı kullanıcı adı veya şifre")
    token = create_access_token(data={"sub": user.username})
    return {"access_token": token, "username": user.username, "user_id": user.id}

@app.post("/upload-csv")
async def upload_csv(
    token: str,
    username: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    verify_token(token)
    db_user = db.query(User).filter(User.username == username).first()
    if not db_user: raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

    user_folder = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_folder): os.makedirs(user_folder)

    # Ağ analizi için dosya network_data.csv olarak kaydedilir
    file_name = "network_data.csv"
    file_path = os.path.join(user_folder, file_name)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # DB Kaydı
    new_analysis = Analysis(user_id=db_user.id, file_name=file_name, analysis_type="manual_csv")
    db.add(new_analysis)
    db.commit()

    return {"status": "success", "file_url": f"/uploads/{username}/{file_name}"}

@app.post("/upload-pdf")
async def process_pdf_analysis(
    token: str,
    username: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    verify_token(token)
    if not GEMINI_CLIENT: raise HTTPException(status_code=500, detail="API Key eksik")

    db_user = db.query(User).filter(User.username == username).first()
    if not db_user: raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

    user_folder = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_folder): os.makedirs(user_folder)
    
    pdf_path = os.path.join(user_folder, "temp.pdf")
    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        doc = fitz.open(pdf_path)
        all_network_data = []
        # Gemini'ye daha net bir talimat
        system_instruction = "Sadece JSON listesi döndür. Örnek: [{\"source\":\"A\", \"target\":\"B\", \"weight\":1}]. Başka açıklama yapma."

        for page in doc:
            text = page.get_text()
            if text.strip():
                try:
                    response = GEMINI_CLIENT.models.generate_content(
                        model=MODEL_NAME,
                        config=types.GenerateContentConfig(system_instruction=system_instruction),
                        contents=text
                    )
                    # JSON Cımbızla Çekme
                    raw = response.text.strip()
                    if "
