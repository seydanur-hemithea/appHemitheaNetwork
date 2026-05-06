
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

from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey
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

if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type('About', (), {'__version__': bcrypt.__version__})

try:
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except Exception as e:
    print(f"Şifreleme sistemi başlatılamadı: {e}")

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
    analysis_type = Column(String) # "pdf_to_hna" veya "manual_csv"
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
app = FastAPI(title="Hemithea Analytics API", version="2.6.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# Statik dosya erişimleri
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")

# --- 5. ENDPOINTLER ---

@app.get("/")
def read_root():
    return {"message": "Hemithea Engine Online", "status": "ready"}

@app.post("/register")
def register(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Kullanıcı mevcut")
    safe_pwd = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    new_user = User(username=username, hashed_password=pwd_context.hash(safe_pwd))
    db.add(new_user)
    db.commit()
    return {"status": "success"}

@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(status_code=401, detail="Kullanıcı yok")
    safe_pwd = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    if not pwd_context.verify(safe_pwd, user.hashed_password):
        raise HTTPException(status_code=401, detail="Hatalı şifre")
    token = create_access_token(data={"sub": user.username})
    return {"access_token": token, "username": user.username, "user_id": user.id}

@app.get("/get-analysis/{username}/{filename}")
async def get_analysis_file(username: str, filename: str, token: str):
    verify_token(token)
    file_path = os.path.join(UPLOAD_DIR, username, filename)
    if not os.path.exists(file_path): raise HTTPException(status_code=404)
    return FileResponse(file_path)

# --- YAKLAŞIM 1: CSV YÜKLEME -> Doğrudan Ağ Analizi (network_data.csv) ---
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

    # Doğrudan ağ analizi için network_data.csv olarak kaydedilir
    file_name = "network_data.csv"
    file_path = os.path.join(user_folder, file_name)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    new_analysis = Analysis(user_id=db_user.id, file_name=file_name, analysis_type="manual_csv")
    db.add(new_analysis)
    db.commit()

    return {"status": "success", "file_url": f"/uploads/{username}/{file_name}"}

# --- YAKLAŞIM 2: PDF YÜKLEME -> HNA Verisi Üretme (hna_data.csv) ---
@app.post("/upload-pdf")
async def process_pdf_analysis(
    token: str,
    username: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    verify_token(token)
    if not GEMINI_CLIENT: raise HTTPException(status_code=500, detail="Gemini API Anahtarı eksik.")

    db_user = db.query(User).filter(User.username == username).first()
    if not db_user: raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

    user_folder = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_folder): os.makedirs(user_folder)
    
    pdf_path = os.path.join(user_folder, "current_analysis.pdf")
    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        doc = fitz.open(pdf_path)
        all_network_data = []
        system_instruction = "Sen bir hiyerarşik ağ analiz uzmanısın. Metinden aktörleri ve ilişkileri bul. Sadece şu formatta JSON döndür: [{\"source\": \"A\", \"target\": \"B\", \"weight\": 1}]"

        for page in doc:
            text = page.get_text()
            if text.strip():
                try:
                    response = GEMINI_CLIENT.models.generate_content(
                        model=MODEL_NAME,
                        config=types.GenerateContentConfig(system_instruction=system_instruction),
                        contents=text
                    )
                    raw_text = response.text.strip()
                    if "```" in raw_text:
                        raw_text = raw_text.split("```")[1]
                        if raw_text.startswith("json"): raw_text = raw_text[4:].strip()
                        raw_text = raw_text.strip()
                    page_data = json.loads(raw_text)
                    if isinstance(page_data, list): all_network_data.extend(page_data)
                except: continue

        doc.close()
        if os.path.exists(pdf_path): os.remove(pdf_path)

        if not all_network_data:
            return {"status": "error", "message": "Analiz verisi çıkarılamadı."}

        df = pd.DataFrame(all_network_data)
        df.columns = [c.lower() for c in df.columns]
        
        if 'source' in df.columns and 'target' in df.columns:
            if 'weight' not in df.columns: df['weight'] = 1
            df = df.groupby(['source', 'target'], as_index=False)['weight'].sum()
            
            # PDF analiz sonucu her zaman hna_data.csv olur
            result_name = "hna_data.csv"
            result_path = os.path.join(user_folder, result_name)
            df.to_csv(result_path, index=False)

            new_analysis = Analysis(user_id=db_user.id, file_name=result_name, analysis_type="pdf_to_hna")
            db.add(new_analysis)
            db.commit()

            return {"status": "success", "file_url": f"/uploads/{username}/{result_name}"}
        else:
            return {"status": "error", "message": "Geçersiz veri formatı üretildi."}

    except Exception as e:
        if os.path.exists(pdf_path): os.remove(pdf_path)
        raise HTTPException(status_code=500, detail=f"Sistem Hatası: {str(e)}")

@app.get("/my-analyses")
def get_user_analyses(token: str, db: Session = Depends(get_db)):
    uname = verify_token(token)
    db_user = db.query(User).filter(User.username == uname).first()
    return db.query(Analysis).filter(Analysis.user_id == db_user.id).all()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

