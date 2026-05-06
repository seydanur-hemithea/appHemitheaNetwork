
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
SECRET_KEY = os.getenv("SECRET_KEY", "Hemithea_Super_Secret_Key_2024")
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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = "gemini-2.0-flash"

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
    
    f_name = "network_data.csv"
    with open(os.path.join(user_path, f_name), "wb") as b: 
        shutil.copyfileobj(file.file, b)
    
    db.add(Analysis(user_id=db_user.id, file_name=f_name, analysis_type="manual_csv"))
    db.commit()
    return {"status": "success", "file_url": f"/uploads/{username}/{f_name}"}

@app.post("/upload-pdf")
async def upload_and_process_pdf(token: str, username: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    verify_token(token)
    if not GEMINI_CLIENT: raise HTTPException(status_code=500, detail="Gemini API Key eksik.")
    db_user = db.query(User).filter(User.username == username).first()
    
    user_path = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_path): os.makedirs(user_path)
    
    temp_pdf = os.path.join(user_path, "temp_proc.pdf")
    with open(temp_pdf, "wb") as b: 
        shutil.copyfileobj(file.file, b)
    
    try:
        doc = fitz.open(temp_pdf)
        extracted_data = []
        inst = "Sadece JSON döndür: [{\"source\":\"A\", \"target\":\"B\", \"weight\":1}]"
        
        for page in doc:
            txt = page.get_text()
            if txt.strip():
                res = GEMINI_CLIENT.models.generate_content(
                    model=MODEL_NAME, 
                    config=types.GenerateContentConfig(system_instruction=inst), 
                    contents=txt
                )
                raw = res.text.strip()
                # Markdown bloklarını temizleme işlemi (bahsettiğin raw kısmı)
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                
                try:
                    data = json.loads(raw)
                    if isinstance(data, list): 
                        extracted_data.extend(data)
                except: 
                    continue
        
        doc.close()
        os.remove(temp_pdf)
        
        if not extracted_data: 
            return {"status": "error", "message": "Veri bulunamadı."}
        
        df = pd.DataFrame(extracted_data)
        df.columns = [c.lower() for c in df.columns]
        df = df.groupby(['source', 'target'], as_index=False)['weight'].sum()
        
        out_name = "hna_data.csv"
        df.to_csv(os.path.join(user_path, out_name), index=False)
        db.add(Analysis(user_id=db_user.id, file_name=out_name, analysis_type="pdf_to_hna"))
        db.commit()
        return {"status": "success", "file_url": f"/uploads/{username}/{out_name}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

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

