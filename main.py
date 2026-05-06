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
from fastapi.responses import FileResponse

from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship

from passlib.context import CryptContext
from jose import JWTError, jwt
import fitz  # PyMuPDF 
from google import genai 
from google import genai 
from google.genai import types 
# --- 1. AYARLAR VE VERİTABANI BAĞLANTISI ---
SECRET_KEY = os.getenv("SECRET_KEY", "gizli_anahtar_buraya")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")
if SQLALCHEMY_DATABASE_URL and "sslmode" not in SQLALCHEMY_DATABASE_URL:
    SQLALCHEMY_DATABASE_URL += "?sslmode=require"

# Bcrypt sürüm uyumluluk yaması
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type('About', (), {'__version__': bcrypt.__version__})

engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

try:
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except Exception as e:
    print(f"Bcrypt başlatılamadı: {e}")

# --- 2. VERİ MODELLERİ (SQLAlchemy) ---
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

# Gemini Client'ı oluşturuyoruz (Render Environment Variables'da GEMINI_API_KEY olmalı)
GEMINI_CLIENT = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL_NAME = "gemini-2.5-flash" # Kullandığımız model
# --- 4. FASTAPI KURULUMU VE CORS ---
app = FastAPI(title="Hemithea Analytics API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# Statik erişim (Eski yöntem - Güvenliksiz)
app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")

# --- 5. ENDPOINTLER ---

@app.get("/")
def home():
    return {"api": "Hemithea Analytics Engine", "status": "active", "version": "2.0"}

# --- KULLANICI İŞLEMLERİ ---
@app.post("/register")
def register(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Kullanıcı zaten mevcut")

    safe_password = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    hashed_password = pwd_context.hash(safe_password)

    new_user = User(username=username, hashed_password=hashed_password)
    db.add(new_user)
    db.commit()
    return {"status": "success", "message": "Kayıt başarılı"}

@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == username).first()
    if not db_user:
        raise HTTPException(status_code=401, detail="Kullanıcı bulunamadı")

    safe_login_password = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    if not pwd_context.verify(safe_login_password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Hatalı şifre")
    
    token = create_access_token(data={"sub": db_user.username})
    return {
        "access_token": token, 
        "token_type": "bearer", 
        "user_id": db_user.id, 
        "username": db_user.username
    }

# --- DOSYA VE ANALİZ İŞLEMLERİ ---

# Streamlit'in veriyi güvenli çekmesini sağlayan GET fonksiyonu
@app.get("/get-analysis/{username}/{filename}")
async def get_analysis_file(username: str, filename: str, token: str):
    try:
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim - Geçersiz Token")

    file_path = os.path.join(UPLOAD_DIR, username, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    
    return FileResponse(file_path)

@app.post("/upload-csv")
async def upload_csv(token: str, username: str = Form(...), file: UploadFile = File(...)):
    try:
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim")

    user_folder = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    file_name = "network_data.csv"
    file_path = os.path.join(user_folder, file_name)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    return {"status": "success", "file_url": f"/get-analysis/{username}/{file_name}"}

# Gemini ile PDF Analizi
GEMINI_CLIENT = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

@app.post("/upload-pdf")
async def process_pdf_analysis(
    token: str, 
    username: str = Form(...), 
    file: UploadFile = File(...), 
    db: Session = Depends(get_db)
):
    try:
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        raise HTTPException(status_code=401, detail="Geçersiz anahtar")

    db_user = db.query(User).filter(User.username == username).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

    user_folder = os.path.join(UPLOAD_DIR, username)
    if os.path.exists(user_folder):
        shutil.rmtree(user_folder)
    os.makedirs(user_folder)
    
    pdf_path = os.path.join(user_folder, "current_analysis.pdf")
    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

      try:
        doc = fitz.open(pdf_path)
        all_network_data = []

        # Gemini'nin veri formatını anlaması için SYSTEM PROMPT
        system_instruction = """
        Sen bir ağ analiz uzmanısın. Metindeki karakterleri/kurumları ve aralarındaki 
        ilişkileri bulup 'source', 'target' ve 'weight' (ilişki gücü) şeklinde 
        JSON formatında döndürmelisin. Sadece JSON döndür.
        """

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            text = page.get_text()
            
            if text.strip():
                # --- GEMINI MODEL ÇAĞRISI BURADA ---
                response = GEMINI_CLIENT.models.generate_content(
                    model=MODEL_NAME,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction
                    ),
                    contents=text
                )
                
                try:
                    # JSON temizleme (Gemini bazen json ekler, onları siliyoruz)
                    raw_text = response.text.strip()
                    if "json" in raw_text:
                        raw_text = raw_text.split("json")[1].split("")[0].strip()
                    
                    page_data = json.loads(raw_text)
                    if isinstance(page_data, list):
                        all_network_data.extend(page_data)
                except Exception as e:
                    print(f"Sayfa {page_num} işlenirken JSON hatası: {e}")
                    continue

        doc.close()
        # ... (Geri kalan DataFrame işlemleri ve CSV kaydetme aynı kalıyor) ...
        
        # DataFrame ve Kayıt Kısmı
        df = pd.DataFrame(all_network_data)
        if not df.empty:
            df = df.groupby(['source', 'target'], as_index=False)['weight'].sum()
            result_csv_name = "hna_data.csv"
            result_csv_path = os.path.join(user_folder, result_csv_name)
            df.to_csv(result_csv_path, index=False)
            
            return {"status": "success", "file_url": f"/get-analysis/{username}/{result_csv_name}"}
        else:
            return {"status": "error", "message": "Analizden veri çıkmadı."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/my-analyses")
def get_user_analyses(token: str, db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        db_user = db.query(User).filter(User.username == username).first()
        return db.query(Analysis).filter(Analysis.user_id == db_user.id).all()
    except:
        raise HTTPException(status_code=401, detail="Geçersiz token")

@app.delete("/delete-account")
def delete_account(token: str, db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        current_user = db.query(User).filter(User.username == username).first()
        if not current_user:
            raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")
        
        db.delete(current_user)
        db.commit()
        
        user_folder = os.path.join(UPLOAD_DIR, username)
        if os.path.exists(user_folder):
            shutil.rmtree(user_folder)
        return {"status": "success", "message": "Hesap silindi"}
    except:
        raise HTTPException(status_code=401, detail="Yetkisiz işlem")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
