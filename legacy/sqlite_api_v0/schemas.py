from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ===== 라즈베리파이 → 서버 =====

class AlertRequest(BaseModel):
    """라즈베리파이에서 보내는 소리 감지 데이터"""
    sound: str              # 감지된 소리 카테고리 (11개 중 하나)
    confidence: float       # 모델 확신도 (0.0 ~ 1.0)
    location: str           # 감지 위치 ("현관", "거실" 등)
    device_id: str          # 기기 식별자 ("rpi-001")
    timestamp: Optional[str] = None  # 감지 시각 (없으면 서버 시각 사용)


class HeartbeatRequest(BaseModel):
    """라즈베리파이 연결 상태 체크"""
    device_id: str
    location: str


# ===== 서버 → 프론트엔드 =====

class UserInfo(BaseModel):
    userName: str
    unreadCount: int


class DeviceStatus(BaseModel):
    name: str
    isConnected: bool


class AlertResponse(BaseModel):
    id: int
    time: str
    location: str
    sound: str
    type: str  # "General" | "Urgent"


class DashboardResponse(BaseModel):
    userInfo: UserInfo
    deviceStatus: list[DeviceStatus]
    alerts: list[AlertResponse]
