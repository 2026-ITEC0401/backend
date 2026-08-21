from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import json

from models import SessionLocal, Alert, Device
from schemas import (
    AlertRequest, HeartbeatRequest,
    DashboardResponse, UserInfo, DeviceStatus, AlertResponse
)
from config import get_alert_type, CONFIDENCE_THRESHOLD, HEARTBEAT_TIMEOUT

app = FastAPI(title="Hearo API", description="청각장애인을 위한 환경음 인식 알림 서버")

# CORS 설정 (프론트엔드에서 접근 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket 연결 관리
connected_clients: list[WebSocket] = []


# ===== WebSocket: 프론트엔드 실시간 연결 =====

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)
    print(f"[WS] 프론트엔드 연결됨 (총 {len(connected_clients)}개)")
    try:
        while True:
            await ws.receive_text()  # 연결 유지
    except WebSocketDisconnect:
        connected_clients.remove(ws)
        print(f"[WS] 프론트엔드 연결 해제 (총 {len(connected_clients)}개)")


async def broadcast_to_frontend(data: dict):
    """연결된 모든 프론트엔드에 실시간 알림 전송"""
    disconnected = []
    for client in connected_clients:
        try:
            await client.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            disconnected.append(client)
    for client in disconnected:
        connected_clients.remove(client)


# ===== POST /api/alerts: 라즈베리파이 → 서버 =====

@app.post("/api/alerts")
async def receive_alert(req: AlertRequest):
    """라즈베리파이에서 감지된 소리 데이터를 수신"""

    # 확신도 체크
    if req.confidence < CONFIDENCE_THRESHOLD:
        return {"status": "ignored", "reason": f"confidence {req.confidence} < {CONFIDENCE_THRESHOLD}"}

    alert_type = get_alert_type(req.sound)

    # DB 저장
    db = SessionLocal()
    try:
        alert = Alert(
            sound=req.sound,
            confidence=req.confidence,
            location=req.location,
            device_id=req.device_id,
            alert_type=alert_type,
            created_at=datetime.now()
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)

        print(f"[알림] {req.location}에서 '{req.sound}' 감지 ({req.confidence*100:.0f}%) [{alert_type}]")

        # 프론트엔드에 실시간 전송
        ws_data = {
            "event": "new_alert",
            "alert": {
                "id": alert.id,
                "time": datetime.now().strftime("%I:%M %p"),
                "location": req.location,
                "sound": req.sound,
                "type": alert_type
            }
        }
        await broadcast_to_frontend(ws_data)

        return {"status": "ok", "alert_id": alert.id, "type": alert_type}
    finally:
        db.close()


# ===== POST /api/heartbeat: 라즈베리파이 연결 상태 =====

@app.post("/api/heartbeat")
def receive_heartbeat(req: HeartbeatRequest):
    """라즈베리파이가 주기적으로 보내는 생존 신호"""
    db = SessionLocal()
    try:
        device = db.query(Device).filter(Device.device_id == req.device_id).first()
        if device:
            device.last_heartbeat = datetime.now()
            device.location = req.location
        else:
            device = Device(
                device_id=req.device_id,
                location=req.location,
                last_heartbeat=datetime.now()
            )
            db.add(device)

        db.commit()
        print(f"[heartbeat] {req.device_id} ({req.location}) 연결 확인")
        return {"status": "ok"}
    finally:
        db.close()


# ===== GET /api/dashboard: 프론트엔드 대시보드 데이터 =====

@app.get("/api/dashboard", response_model=DashboardResponse)
def get_dashboard():
    """프론트엔드 대시보드용 종합 데이터 (API 명세서 형식)"""
    db = SessionLocal()
    try:
        now = datetime.now()
        cutoff = now - timedelta(seconds=HEARTBEAT_TIMEOUT)

        # 기기 연결 상태
        devices = db.query(Device).all()
        device_status = [
            DeviceStatus(
                name=d.location,
                isConnected=d.last_heartbeat >= cutoff
            )
            for d in devices
        ]

        # 미확인 알림 수
        unread_count = db.query(Alert).filter(Alert.is_read == False).count()

        # 최근 알림 50개
        recent_alerts = db.query(Alert).order_by(Alert.created_at.desc()).limit(50).all()
        alerts = [
            AlertResponse(
                id=a.id,
                time=a.created_at.strftime("%I:%M %p"),
                location=a.location,
                sound=a.sound,
                type=a.alert_type
            )
            for a in recent_alerts
        ]

        return DashboardResponse(
            userInfo=UserInfo(userName="사용자", unreadCount=unread_count),
            deviceStatus=device_status,
            alerts=alerts
        )
    finally:
        db.close()


# ===== PATCH /api/alerts/{alert_id}/read: 알림 읽음 처리 =====

@app.patch("/api/alerts/{alert_id}/read")
def mark_alert_read(alert_id: int):
    """알림을 읽음 처리"""
    db = SessionLocal()
    try:
        alert = db.query(Alert).filter(Alert.id == alert_id).first()
        if not alert:
            return {"status": "error", "message": "알림을 찾을 수 없습니다"}
        alert.is_read = True
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("  Hearo 서버 시작")
    print("  API 문서: http://localhost:8000/docs")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)
