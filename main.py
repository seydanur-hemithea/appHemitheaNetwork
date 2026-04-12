import os
import shutil
import time
from datetime import datetime, timedelta
from typing import List
import uvicorn

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, status, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext


# Passlib'in bcrypt hatasını çözmek için küçük bir yama
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type('About', (), {'__version__': bcrypt.__version__})

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, "hemithea.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{db_path}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False}
)
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
    try:
        # Önce kullanıcı var mı kontrolü
        db_user = db.query(User).filter(User.username == username).first()
        if db_user:
            raise HTTPException(status_code=400, detail="User already registered")

        # Şifreleme işlemi
        hashed_pw = pwd_context.hash(password[:72])
        
        # Yeni kullanıcı ekleme
        new_user = User(username=username, hashed_password=hashed_pw)
        db.add(new_user)
        db.commit()
        # Refresh bazen sorun çıkarabilir, opsiyoneldir
        # db.refresh(new_user) 
        
        return {"status": "success", "message": "Kayıt başarılı"}
    except Exception as e:
        print(f"REGISTER HATASI: {str(e)}")
        raise HTTPException(status_code=500, detail="Sunucu kaydı yapamadı.")
    
@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    try:
        db_user = db.query(User).filter(User.username == username).first()
        
        if not db_user:
            raise HTTPException(status_code=401, detail="Kullanıcı bulunamadı")

        # Şifre kontrolü yaparken hata oluşursa 'except' bloğuna düşecek
        is_verified = pwd_context.verify(password[:72], db_user.hashed_password)
        
        if not is_verified:
            raise HTTPException(status_code=401, detail="Hatalı şifre")
        
        return {"user_id": db_user.id, "username": db_user.username}

    except Exception as e:
        # Bu satır sayesinde hatayı Render loglarında kabak gibi göreceğiz
        print(f"KRİTİK LOGIN HATASI: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Sunucu içi hata: {str(e)}")
@app.post("/upload-csv")
async def upload_file(
    background_tasks: BackgroundTasks,
    user_id: int = Form(...), 
    file: UploadFile = File(...), 
    db: Session = Depends(get_db)
):
    # Kullanıcıya özel dosya ismi (seyda_123_data.csv gibi)
    timestamp = int(time.time())
    file_name = f"user_{user_id}_{timestamp}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # Analizi veritabanına kaydet (Geçici olarak)
    new_analysis = Analysis(user_id=user_id, file_name=file_name)
    db.add(new_analysis)
    db.commit()
    db.refresh(new_analysis)
    
    # Arka plan görevini başlat: 3 saat sonra silme kontrolü
    background_tasks.add_task(delete_expired_file, file_path, new_analysis.id)
    
    return {
        "status": "success", 
        "file_url": f"/uploads/{file_name}",
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

@app.get("/my-analyses/{user_id}")
def get_user_analyses(user_id: int, db: Session = Depends(get_db)):
    analyses = db.query(Analysis).filter(Analysis.user_id == user_id).all()
    return analyses
    
if __name__ == "__main__":
    # Render'ın verdiği portu al, eğer yoksa (lokaldeyken) 8000 kullan
    port = int(os.environ.get("PORT", 8000))
    
    # Host mutlaka "0.0.0.0" olmalı, yoksa dış dünyadan erişilemez
    uvicorn.run(app, host="0.0.0.0", port=port)
