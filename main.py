import httpx

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

# --- 1. VERİTABANI AYARLARI ---
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

if SQLALCHEMY_DATABASE_URL and "sslmode" not in SQLALCHEMY_DATABASE_URL:
    SQLALCHEMY_DATABASE_URL += "?sslmode=require"

engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- 2. MODELLER ---
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
    analysis_type = Column(String, nullable=True) # PDF_TO_HNA veya MANUAL_CSV
    created_at = Column(DateTime, default=datetime.utcnow)

# Otomatik Tablo Güncelleme (Migration)
Base.metadata.create_all(bind=engine)
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS analysis_type VARCHAR"))
        conn.commit()
    except Exception as e:
        print(f"Migration log: {e}")

# --- 3. GÜVENLİK VE API ---
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
        raise HTTPException(status_code=401, detail="Yetkisiz erişim")
NLP_SERVICE_URL = os.getenv("NLP_SERVICE_URL") + "/analyze"



app = FastAPI(title="Hemithea Engine", version="2.6.5")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR): os.makedirs(UPLOAD_DIR)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- 4. ENDPOINTLER ---

@app.get("/")
def health_check():
    return {"status": "online", "engine": "Hemithea 2.6.5"}

@app.post("/register")
def register_user(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Bu kullanıcı zaten var.")
    new_user = User(username=username, hashed_password=pwd_context.hash(password))
    db.add(new_user)
    db.commit()
    return {"status": "success"}

@app.post("/login")
def login_user(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Hatalı bilgiler.")
    token = create_access_token(data={"sub": user.username})
    return {"access_token": token, "username": user.username, "user_id": user.id}


@app.post("/upload-csv")
async def upload_manual_csv(token: str, username: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    verify_token(token)
    db_user = db.query(User).filter(User.username == username).first()
    if not db_user: raise HTTPException(status_code=404)
    
    user_path = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_path): os.makedirs(user_path)

    out_path = os.path.join(user_path, "hna_data.csv")
    if os.path.exists(out_path):
        os.remove(out_path) # Yeni analiz başlarken eskiyi temizle

    db_user = db.query(User).filter(User.username == username).first()
    temp_pdf = os.path.join(user_path, "temp_proc.pdf")

    
    f_name = "network_data.csv"
    with open(os.path.join(user_path, f_name), "wb") as b: 
        shutil.copyfileobj(file.file, b)
    
    db.add(Analysis(user_id=db_user.id, file_name=f_name, analysis_type="manual_csv"))
    db.commit()
    return {"status": "success", "file_url": f"/uploads/{username}/{f_name}"}
@app.post("/upload-pdf")
async def upload_and_process_pdf(
    token: str, 
    username: str = Form(...), 
    file: UploadFile = File(...), 
    db: Session = Depends(get_db)
):
    # 1. AUTH (Kullanıcı Doğrulama)
    user_name_from_token = verify_token(token)
    if not user_name_from_token or user_name_from_token != username:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim.")

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")

    # 2. KLASÖR VE DOSYA YOLLARI
    user_path = os.path.join(UPLOAD_DIR, username)
    os.makedirs(user_path, exist_ok=True)
    
    out_path = os.path.join(user_path, "hna_data.csv")
    temp_pdf = os.path.join(user_path, "temp_proc.pdf")

    # 3. PDF'İ KAYDET VE METNİ ÇIKAR
    try:
        with open(temp_pdf, "wb") as b: 
            shutil.copyfileobj(file.file, b)

        doc = fitz.open(temp_pdf)
        full_text = ""
        # Sayfa sınırı (Bellek yönetimi için önemli)
        max_p = min(len(doc), 100) 
        for i in range(max_p):
            full_text += doc[i].get_text() + "\n"
        doc.close()

        if not full_text.strip():
            raise HTTPException(status_code=400, detail="PDF metni okunamadı.")

        # 4. NLP SERVİSİNE (Hugging Face) GÖNDER
        async with httpx.AsyncClient() as client:
            response = await client.post(
                NLP_SERVICE_URL, 
                json={"text": full_text},
                timeout=300.0 # 5 dakika (Büyük analizler için)
            )
        
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="NLP servisi hata döndürdü.")

        # 5. VERİYİ CSV OLARAK KAYDET
        all_network_response = response.json()
        
        # Hugging Face'den gelen verinin formatını kontrol et
        if all_network_response.get("status") == "success":
            network_list = all_network_response.get("network", [])
            
            if network_list:
                df = pd.DataFrame(network_list)
                df.columns = [c.lower() for c in df.columns]
                
                # Aynı karakter çiftlerini toplayarak ağırlıklandır (Aggregation)
                df = df.groupby(['source', 'target'], as_index=False)['weight'].sum()
                df.to_csv(out_path, index=False, encoding='utf-8-sig') # Excel dostu UTF-8
                
                # Veritabanı kaydı
                db.add(Analysis(user_id=user.id, file_name=file.filename, analysis_type="PDF_TO_HNA"))
                db.commit()

                return {
                    "status": "success", 
                    "file_url": f"/uploads/{username}/hna_data.csv"
                }
        
        return {"status": "error", "message": "Analiz sonucu boş döndü."}

    except Exception as e:
        print(f"HNA_LOG_ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Hata olsa da olmasa da geçici dosyayı temizle
        if os.path.exists(temp_pdf):
            os.remove(temp_pdf)


        # 6. VERİYİ CSV OLARAK KAYDETME
        if all_network_data:
            df = pd.DataFrame(all_network_data)
            df.columns = [c.lower() for c in df.columns]
            # Kaynak-Hedef bazlı gruplayıp ağırlıkları topla
            df = df.groupby(['source', 'target'], as_index=False)['weight'].sum()
            df.to_csv(out_path, index=False)
            
            # Veritabanına Analiz Kaydını Ekle
            new_analysis = Analysis(
                user_id=user.id, 
                file_name=file.filename, 
                analysis_type="PDF_TO_HNA"
            )
            db.add(new_analysis)
            db.commit()

            # Geçici dosyayı temizle
            if os.path.exists(temp_pdf): os.remove(temp_pdf)

            return {
                "status": "success", 
                "message": "Analiz tamamlandı.",
                "file_url": f"/uploads/{username}/hna_data.csv"
            }
        
        return {"status": "error", "message": "Karakter ilişkisi bulunamadı."}

    except Exception as e:
        # Hata durumunda geçici PDF'i mutlaka sil ki yer kaplamasın
        if os.path.exists(temp_pdf): os.remove(temp_pdf)
        print(f"HATA: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

        
@app.get("/my-analyses")
def list_analyses(token: str, db: Session = Depends(get_db)):
    uname = verify_token(token)
    u = db.query(User).filter(User.username == uname).first()
    return db.query(Analysis).filter(Analysis.user_id == u.id).all()

@app.delete("/delete-account")
def delete_user(token: str, db: Session = Depends(get_db)):
    uname = verify_token(token)
    u = db.query(User).filter(User.username == uname).first()
    shutil.rmtree(os.path.join(UPLOAD_DIR, uname), ignore_errors=True)
    db.delete(u)
    db.commit()
    return {"status": "success"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

