
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

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        raise HTTPException(status_code=401, detail="Geçersiz anahtar.")

# API Key kontrolü
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = "gemini-2.0-flash"

# --- 4. FASTAPI UYGULAMASI ---
app = FastAPI(title="Hemithea Analytics API", version="2.5.4")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")

# --- 5. ENDPOINTLER ---

@app.get("/")
def read_root():
    return {"message": "Hemithea Engine Online", "gemini_status": "configured" if GEMINI_CLIENT else "missing_key"}

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
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    temp_path = os.path.join(user_folder, "temp_upload.csv")
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    try:
        df = pd.read_csv(temp_path)
        if len(df.columns) >= 3:
            df.columns = ['source', 'target', 'weight'] + list(df.columns[3:])
            df_clean = df[['source', 'target', 'weight']].copy()
            df_clean['weight'] = pd.to_numeric(df_clean['weight'], errors='coerce').fillna(1)
            
            final_name = "hna_data.csv"
            final_path = os.path.join(user_folder, final_name)
            df_clean.to_csv(final_path, index=False)
            
            new_analysis = Analysis(user_id=db_user.id, file_name=final_name)
            db.add(new_analysis)
            db.commit()
            
            os.remove(temp_path)
            return {"status": "success", "file_url": f"/get-analysis/{username}/{final_name}"}
        else:
            os.remove(temp_path)
            return {"status": "error", "message": "CSV en az 3 sütun içermeli."}
    except Exception as e:
        if os.path.exists(temp_path): os.remove(temp_path)
        raise HTTPException(status_code=400, detail=f"CSV Hatası: {str(e)}")

@app.post("/upload-pdf")
async def process_pdf_analysis(
    token: str,
    username: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    verify_token(token)
    
    if not GEMINI_CLIENT:
        raise HTTPException(status_code=500, detail="Gemini API Anahtarı sunucuda eksik (GEMINI_API_KEY).")

    db_user = db.query(User).filter(User.username == username).first()
    if not db_user: raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

    user_folder = os.path.join(UPLOAD_DIR, username)
    if os.path.exists(user_folder): shutil.rmtree(user_folder)
    os.makedirs(user_folder)
    
    pdf_path = os.path.join(user_folder, "current_analysis.pdf")
    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        doc = fitz.open(pdf_path)
        all_network_data = []

        system_instruction = "Sen bir ağ analiz uzmanısın. Metinden aktörleri ve ilişkileri bul. Sadece şu formatta JSON döndür: [{\"source\": \"A\", \"target\": \"B\", \"weight\": 1}]"

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
                    # Markdown temizliği
                    if "```" in raw_text:
                        raw_text = raw_text.split("```")[1]
                        if raw_text.startswith("json"):
                            raw_text = raw_text[4:].strip()
                        raw_text = raw_text.strip()

                    page_data = json.loads(raw_text)
                    if isinstance(page_data, list):
                        all_network_data.extend(page_data)
                except Exception as inner_e:
                    print(f"Sayfa işleme hatası: {inner_e}")
                    continue

        doc.close()
        if os.path.exists(pdf_path): os.remove(pdf_path)

        if not all_network_data:
            return {"status": "error", "message": "PDF içeriğinden analiz edilebilir veri çıkarılamadı."}

        df = pd.DataFrame(all_network_data)
        # Sütunları standartlaştır (Küçük harf vs)
        df.columns = [c.lower() for c in df.columns]
        
        if 'source' in df.columns and 'target' in df.columns:
            if 'weight' not in df.columns: df['weight'] = 1
            df = df.groupby(['source', 'target'], as_index=False)['weight'].sum()
            
            result_csv_name = "hna_data.csv"
            result_csv_path = os.path.join(user_folder, result_csv_name)
            df.to_csv(result_csv_path, index=False)

            new_analysis = Analysis(user_id=db_user.id, file_name=result_csv_name)
            db.add(new_analysis)
            db.commit()

            return {"status": "success", "file_url": f"/get-analysis/{username}/{result_csv_name}"}
        else:
            return {"status": "error", "message": "Gemini uygun formatta veri üretmedi."}

    except Exception as e:
        if os.path.exists(pdf_path): os.remove(pdf_path)
        raise HTTPException(status_code=500, detail=f"Sistem Hatası: {str(e)}")

@app.get("/my-analyses")
def get_user_analyses(token: str, db: Session = Depends(get_db)):
    uname = verify_token(token)
    db_user = db.query(User).filter(User.username == uname).first()
    return db.query(Analysis).filter(Analysis.user_id == db_user.id).all()

@app.delete("/delete-account")
def delete_account(token: str, db: Session = Depends(get_db)):
    uname = verify_token(token)
    db_user = db.query(User).filter(User.username == uname).first()
    shutil.rmtree(os.path.join(UPLOAD_DIR, uname), ignore_errors=True)
    db.delete(db_user)
    db.commit()
    return {"status": "success"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

