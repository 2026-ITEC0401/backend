
"""
=============================================================
Hearo 웹 대시보드 연동 API 서버
=============================================================
EC2에서 실행되며, DynamoDB(hearo-db)의 알림 데이터를
웹 대시보드가 조회할 수 있도록 제공합니다.

인증: EC2에 연결된 IAM 역할(SafeRole) 사용 — 액세스 키 불필요

엔드포인트
  GET /health         : 서버 상태 확인
  GET /alarms/latest  : 최신 알림 1건 (대시보드 폴링용)
  GET /alarms?limit=N : 최신 알림 N건 (초기 로딩용, 기본 20건)

실행: uvicorn dashboard_api:app --host 0.0.0.0 --port 8000
=============================================================
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from boto3.dynamodb.conditions import Key
from decimal import Decimal
import boto3

# ============================================================
# 설정
# ============================================================
REGION = "ap-south-1"
TABLE_NAME = "hearo-db"
DEVICE_ID = "rpi-001"          # 현재 운영 중인 라즈베리파이 허브

# CORS 허용 주소 — 프론트엔드 배포 후 실제 주소 추가 필요
ALLOWED_ORIGINS = [
    "http://localhost:5173",    # Vite 개발 서버 기본 포트
    "http://localhost:3000",
    "*",                        # ⚠️ 시연용. 배포 주소 확정 시 이 줄 삭제 권장
]

# ============================================================
# 초기화
# ============================================================
app = FastAPI(title="Hearo Dashboard API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)


# ============================================================
# 내부 함수
# ============================================================
def to_alert(item: dict) -> dict:
    """
    DynamoDB 항목을 대시보드가 사용하는 형태로 변환합니다.

    프론트엔드(MainPage.jsx)가 사용하는 필드에 맞춰 최소한으로 구성:
      id, time, location, sound, type

    · id   : DynamoDB에 저장된 값이 있으면 사용,
             없으면 device_id#timestamp 조합으로 생성 (React key 용도)
    · time : ISO 8601 문자열 그대로 전달
             → 프론트에서 new Date(문자열)로 파싱 (요청안 참고)
    """
    device_id = item.get("device_id", "")
    timestamp = item.get("timestamp", "")

    return {
        "id": item.get("id") or f"{device_id}#{timestamp}",
        "time": timestamp,
        "location": item.get("location", ""),
        "sound": item.get("sound", ""),
        "type": item.get("type", ""),
        "confidence": float(item["confidence"]) if isinstance(
            item.get("confidence"), Decimal
        ) else item.get("confidence"),
    }


def fetch_alarms(limit: int) -> list:
    """
    최신순으로 알림을 조회합니다.

    device_id를 파티션 키로 지정하고 ScanIndexForward=False를 주면
    정렬 키(timestamp) 역순, 즉 최신순으로 반환됩니다.
    Scan이 아닌 Query를 사용하므로 데이터가 쌓여도 성능이 유지됩니다.
    """
    response = table.query(
        KeyConditionExpression=Key("device_id").eq(DEVICE_ID),
        ScanIndexForward=False,
        Limit=limit,
    )
    return [to_alert(item) for item in response.get("Items", [])]


# ============================================================
# 엔드포인트
# ============================================================
@app.get("/health")
def health():
    """서버 및 DynamoDB 연결 상태를 확인합니다."""
    try:
        table.table_status
        return {"status": "ok", "table": TABLE_NAME, "region": REGION}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DynamoDB 연결 실패: {e}")


@app.get("/alarms/latest")
def get_latest_alarm():
    """
    최신 알림 1건을 반환합니다. (대시보드 폴링용)

    기존 Firestore의 onSnapshot + limit(1) 구독을 대체합니다.
    알림이 하나도 없으면 alarm은 null입니다.

    응답 예시:
    {
      "alarm": {
        "id": "rpi-001#2026-08-07T10:30:00+00:00",
        "time": "2026-08-07T10:30:00+00:00",
        "location": "거실",
        "sound": "노크소리",
        "type": "Visitor",
        "confidence": 0.87
      }
    }
    """
    try:
        alarms = fetch_alarms(limit=1)
        return {"alarm": alarms[0] if alarms else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"조회 실패: {e}")


@app.get("/alarms")
def get_alarms(limit: int = Query(default=20, ge=1, le=100)):
    """
    최신 알림 목록을 반환합니다. (페이지 초기 로딩용)

    응답 예시:
    {
      "alarms": [ { ...위와 동일한 형태... }, ... ],
      "count": 5
    }
    """
    try:
        alarms = fetch_alarms(limit=limit)
        return {"alarms": alarms, "count": len(alarms)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"조회 실패: {e}")
