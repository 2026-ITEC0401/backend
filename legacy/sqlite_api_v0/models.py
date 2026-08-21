from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite:///./hearo.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Alert(Base):
    """소리 감지 알림 기록"""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sound = Column(String, nullable=False)          # 감지된 소리
    confidence = Column(Float, nullable=False)       # 확신도
    location = Column(String, nullable=False)        # 위치
    device_id = Column(String, nullable=False)       # 기기 ID
    alert_type = Column(String, nullable=False)      # "General" | "Urgent"
    is_read = Column(Boolean, default=False)         # 사용자 확인 여부
    created_at = Column(DateTime, default=datetime.now)


class Device(Base):
    """라즈베리파이 기기 등록 및 상태"""
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String, unique=True, nullable=False)
    location = Column(String, nullable=False)
    last_heartbeat = Column(DateTime, default=datetime.now)


# 테이블 생성
Base.metadata.create_all(bind=engine)
