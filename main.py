import os
import shutil
import time
from datetime import datetime, timedelta
from typing import List
import uvicorn
import bcrypt
import psycopg2

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, status, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import JWTError, jwt

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("JWT_ALGORITHM")
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

# Passlib'in bcrypt hatasını çözmek için küçük bir yama
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type('About', (), {'__version__': bcrypt.__version__})


engine = create_engine(
    SQLALCHEMY_DATABASE_URL)


# Token Üretme Fonksiyonu
def create_access_token(data: dict):
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Şifreleme ayarını daha basit ve hata vermez hale getirelim
try:
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except Exception as e:
    print(f"Bcrypt başlatılamadı: {e}")

# --- 2. VERİ MODELLERİ (Tablolar) ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)

class Analysis(Base):
    __tablename__ = "analyses"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
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

def delete_expired_file(file_path: str, analysis_id: int):
    """3 saat bekler ve eğer analiz kaydedilmemişse dosyayı siler."""
    time.sleep(10800) # 3 saat (10800 saniye)
    db = SessionLocal()
    analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
    
    if analysis and not analysis.is_saved:
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Süre doldu: {file_path} silindi.")
    db.close()

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

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- 5. ENDPOINTLER (Yollar) ---



@app.post("/register")
def register(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    # Kullanıcı kontrolü...
    db_user = db.query(User).filter(User.username == username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="User already registered")

    # --- GÜVENLİ ŞİFRELEME (72 BYTE SINIRI İÇİN) ---
    # Şifreyi byte'a çevirip ilk 72 byte'ı alıyoruz
    password_bytes = password.encode('utf-8')
    safe_password_bytes = password_bytes[:72]
    # Tekrar string'e çeviriyoruz ki passlib hata vermesin
    safe_password = safe_password_bytes.decode('utf-8', errors='ignore')

    hashed_password = pwd_context.hash(safe_password)
    # -----------------------------------------------

    new_user = User(username=username, hashed_password=hashed_password)
    db.add(new_user)
    db.commit()
    return {"status": "success"}
    
@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == username).first()
    
    # 1. Kullanıcı var mı kontrol et
    if not db_user:
        raise HTTPException(status_code=401, detail="Kullanıcı bulunamadı")

    # 2. Şifreyi doğrula (Güvenli 72 byte kuralıyla)
    safe_login_password = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    if not pwd_context.verify(safe_login_password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Hatalı şifre")
    
    # 3. Giriş başarılıysa Token üret
    access_token = create_access_token(data={"sub": db_user.username})
   
    return {
        "access_token": access_token, 
        "token_type": "bearer", 
        "user_id": db_user.id,
        "username": db_user.username  # <--- Bunu ekle ki Android 'null' demesin!
    }
@app.post("/upload-csv")    
async def upload_file(
    background_tasks: BackgroundTasks,
    token:str
    username: str = Form(...), 
    file: UploadFile = File(...), 
    db: Session = Depends(get_db)
):
try:
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        raise HTTPException(status_code=401, detail="Geçersiz anahtar! Lütfen tekrar giriş yapın.")

    # 1. Kullanıcı kontrolü
    db_user = db.query(User).filter(User.username == username).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")
        
    # 2. Kullanıcıya özel klasör yolu (Örn: uploads/seydanur)
    user_folder = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    # 3. Sabit dosya ismi (Streamlit'in aradığı isim)
    file_name = "network_data.csv"
    file_path = os.path.join(user_folder, file_name)
    
    # Dosyayı kaydet (Üzerine yazar, böylece klasör şişmez)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # 4. Veritabanı kaydı
    new_analysis = Analysis(
        user_id=db_user.id, 
        file_name=f"{username}/{file_name}"
    )
    db.add(new_analysis)
    db.commit()
    db.refresh(new_analysis)
    
    # Arka plan görevi (3 saat sonra silme - istersen aktif kalabilir)
    background_tasks.add_task(delete_expired_file, file_path, new_analysis.id)
    
    return {
        "status": "success", 
        "file_url": f"/uploads/{username}/{file_name}",
        "analysis_id": new_analysis.id
    }
@app.post("/save-analysis/{analysis_id}")
def save_analysis(analysis_id: int, db: Session = Depends(get_db)):
    analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
    if not analysis:
        raise HTTPException(status_code=404, detail="Analiz bulunamadı.")
    
    analysis.is_saved = True
    db.commit()
    return {"status": "success", "message": "Analiz kalıcı olarak kaydedildi."}

@app.get("/my-analyses")
def get_user_analyses(token: str, db: Session = Depends(get_db)):
    try:
        # Token'ı çöz ve içindeki kullanıcıyı bul
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        
        db_user = db.query(User).filter(User.username == username).first()
        analyses = db.query(Analysis).filter(Analysis.user_id == db_user.id).all()
        return analyses
    except JWTError:
        raise HTTPException(status_code=401, detail="Geçersiz anahtar! Lütfen giriş yapın.")
@app.get("/")
def home():
    # Sadece sistemin durumu hakkında bilgi verir
    return {
        "api_name": "Hemithea Analytics Engine",
        "status": "active",
        "environment": "production"
    }


    
if __name__ == "__main__":
    # Render'ın verdiği portu al, eğer yoksa (lokaldeyken) 8000 kullan
    port = int(os.environ.get("PORT", 8000))
    
    # Host mutlaka "0.0.0.0" olmalı, yoksa dış dünyadan erişilemez
    uvicorn.run(app, host="0.0.0.0", port=port)
