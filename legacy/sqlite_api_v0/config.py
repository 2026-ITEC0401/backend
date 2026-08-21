# 11개 카테고리 목록 (YAMNet 파인튜닝 모델 기준)
CATEGORIES = [
    "가전제품", "개 짖음", "노크 소리", "도어락", "물 소리",
    "사이렌", "아기 울음", "인터폰", "초인종", "창문 깨지는 소리", "휴대폰 벨소리"
]

# 긴급 카테고리 (프론트엔드 type: "Urgent")
URGENT_CATEGORIES = {"사이렌", "창문 깨지는 소리", "아기 울음"}

# 기기 연결 타임아웃 (초) — 이 시간 안에 heartbeat가 안 오면 연결 끊김 처리
HEARTBEAT_TIMEOUT = 60

# threshold (라즈베리파이에서 이미 필터링하지만 서버에서도 이중 체크)
CONFIDENCE_THRESHOLD = 0.9


def get_alert_type(sound: str) -> str:
    """카테고리 이름으로 긴급도 반환"""
    return "Urgent" if sound in URGENT_CATEGORIES else "General"
