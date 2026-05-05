import os
import json
import pandas as pd

import shutil
import time
from datetime import datetime, timedelta
from typing import List
import uvicorn
import bcrypt
import psycopg2
import asyncio

from sqlalchemy import ForeignKey
from sqlalchemy.orm import relationship
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, status, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import JWTError, jwt
import fitz  # PyMuPDF 
from google import genai # NLP asistanındaki yeni kütüphane


SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("JWT_ALGORITHM")
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL") + "?sslmode=require"


# Passlib'in bcrypt hatasını çözmek için küçük bir yama
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type('About', (), {'__version__': bcrypt.__version__})


engine = create_engine(
    SQLALCHEMY_DATABASE_URL,pool_pre_ping=True)


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

async def delete_expired_file(file_path: str, analysis_id: int):
    # 3 saat bekle ama bloklama yok
    await asyncio.sleep(10800)  
    
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
    token: str,
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


# --- GEMINI CLIENT TANIMLAMASI (API_KEY'i os.getenv ile alıyoruz) ---
GEMINI_CLIENT = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

@app.post("/upload-pdf")
async def process_pdf_analysis(
    token: str,
    username: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    # 1. Token ve Kullanıcı Kontrolü (Senin mevcut mantığınla aynı)
    try:
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        raise HTTPException(status_code=401, detail="Geçersiz anahtar!")

    db_user = db.query(User).filter(User.username == username).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")

    # 2. Dosyayı Geçici Olarak Kaydet
    user_folder = os.path.join(UPLOAD_DIR, username)
    if not os.path.exists(user_folder): os.makedirs(user_folder)
    
    pdf_path = os.path.join(user_folder, "current_analysis.pdf")
    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 3. --- HNA CORE ENGINE BAŞLIYOR (Senin müthiş mantığın) ---
    try:
        doc = fitz.open(pdf_path)
        all_network_data = []
        step = 20  # Sayfa atlama aralığı
        max_pages = min(100, len(doc)) # İlk 100 sayfa sınırı

        for i in range(0, max_pages, step):
            text = ""
            for page_num in range(i, min(i + step, max_pages)):
                text += doc[page_num].get_text()
            
            prompt = f"""
Metindeki karakterleri sosyal ağ analizi için ayıkla.
YALNIZCA şu formatta geçerli bir JSON listesi döndür:
[ {{"source": "Karakter A", "target": "Karakter B", "weight": 1}} ]
JSON dışında hiçbir açıklama veya metin ekleme.
Metin: {text}
"""


            response = GEMINI_CLIENT.models.generate_content(
                model="gemini-2.5-flash", # En hızlı ve güncel model
                contents=prompt
            )
            
            # JSON Temizleme
            raw_json = response.text.strip()
            if "```" in raw_json:
                raw_json = raw_json.split("```")[1].replace("json", "").strip()
            
            try:
                batch_data = json.loads(raw_json)
                all_network_data.extend(batch_data)
            except:
                continue 

        doc.close()

        # 4. Verileri DataFrame ile Birleştir ve CSV Olarak Kaydet
        df = pd.DataFrame(all_network_data)
        if not df.empty:
            df = df.groupby(['source', 'target'], as_index=False)['weight'].sum()
        
        # Sonuç CSV'sini Streamlit'in göreceği yere yazıyoruz
        result_csv_name = "hna_total_network.csv"
        result_csv_path = os.path.join(user_folder, result_csv_name)
        df.to_csv(result_csv_path, index=False)

        # 5. Veritabanına Analiz Kaydı
        new_analysis = Analysis(
            user_id=db_user.id, 
            file_name=f"{username}/{result_csv_name}"
        )
        db.add(new_analysis)
        db.commit()

        return {
            "status": "success",
            "message": "Derin analiz tamamlandı, ağ haritası oluşturuldu.",
            "analysis_id": new_analysis.id,
            "data_preview": df.head(5).to_dict(orient="records") # Android'e küçük bir önizleme
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analiz motoru hatası: {str(e)}")

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

@app.delete("/delete-account")
def delete_account(token: str, db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        current_user = db.query(User).filter(User.username == username).first()
    except JWTError:
        raise HTTPException(status_code=401, detail="Geçersiz token!")

    if not current_user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")

    # 1. Analizleri DB’den sil
    analyses = db.query(Analysis).filter(Analysis.user_id == current_user.id).all()
    for analysis in analyses:
        db.delete(analysis)

    # 2. Kullanıcıyı DB’den sil
    db.delete(current_user)
    db.commit()   # <-- önce DB commit

    # 3. Dosyaları diskten sil (DB’den bağımsız)
    for analysis in analyses:
        file_path = os.path.join(UPLOAD_DIR, analysis.file_name)
        if os.path.exists(file_path):
            os.remove(file_path)

    user_folder = os.path.join(UPLOAD_DIR, username)
    if os.path.exists(user_folder):
        shutil.rmtree(user_folder)

    return {"status": "success", "message": "Hesabınız ve tüm verileriniz silindi."}


    

