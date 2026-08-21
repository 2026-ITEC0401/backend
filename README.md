# Hearo 백엔드 v2

구현 기준일은 2026-08-21입니다. 이 소스는 샘플 단계의 이메일·일회용 초대 코드·7일 이력 계약을 제거하고, 프론트팀과 합의한 최종 계약을 구현합니다. AWS EC2의 FastAPI, DynamoDB, MQTT TLS 브리지를 이용한 운영 smoke test를 완료했습니다.

- API 계약: [`docs/API_SPEC_V2_DRAFT.md`](./docs/API_SPEC_V2_DRAFT.md)
- 기존 샘플과의 차이: [`docs/API_SPEC_V2_SAMPLE_GAP.md`](./docs/API_SPEC_V2_SAMPLE_GAP.md)
- 배포와 롤백: [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md)

## 구현된 기능

- 별도 `login_id`, 이름, 휴대폰 번호, 비밀번호 기반 회원가입·로그인
- Argon2id 비밀번호 해시, access JWT 15분, refresh JWT 30일과 회전·로그아웃
- 신규 가구 owner 가입과 가족·보호자 미연동 가입
- 24시간 유효한 6자리 공유 초대 코드, 가구 미리보기와 추후 연동
- 사용자별 가족 표시 이름과 본인 계정의 가구 연동 해제
- owner 연동 해제 시 가구 비활성화 및 긴급 주소 삭제
- 신규 가구의 도로명주소·우편번호·상세주소 저장과 권한 제한 조회
- 고정 4개 기기의 MQTT 연결·LED 설정, heartbeat와 HTTPS config polling
- 최근 30일 KST 날짜별 알림, 최신 알림, 특정 알림 상세 조회
- 가구별 WebSocket 기기 상태·신규 알림 전달
- 보호자 연락처, device credential, MQTT 수집기
- 공통 오류 응답과 로그인·초대 코드 요청 제한

진동, 민감도, 녹음 재생, 세탁 완료 알림, 이메일/SES 비밀번호 재설정은 최종 범위에 포함하지 않습니다.

## 주요 파일

| 파일·폴더 | 역할 |
|---|---|
| `dashboard_api_v2.py` | 기존 배포 명령과 호환되는 FastAPI 진입점 |
| `hearo_backend/main.py` | API 라우트, 인증·권한, WebSocket |
| `hearo_backend/services.py` | 회원가입·로그인·초대·30일 이력 서비스 |
| `hearo_backend/store.py` | 메모리 개발 저장소와 DynamoDB 운영 저장소 |
| `hearo_backend/mqtt_bridge.py` | MQTT 상태·알림을 내부 API로 전달 |
| `infra/aws-v2.yaml` | 최종 core·alerts 테이블과 EC2 최소 권한 역할 |
| `infra/systemd/hearo-api-v2-final.service` | 기존 v1 옆의 8001 병렬 배포 예시 |
| `infra/nginx/hearo-api-v2-location.conf` | HTTPS `/v2/` 프록시 예시 |
| `tests/` | 인증, 가구, 주소, 기기, WebSocket, 알림, Dynamo 계약 테스트 |
| `legacy/` | v2 이전 SQLite API와 AWS 대시보드 API 보존본 |

## 저장소 구조

```text
backend/
├── dashboard_api_v2.py          # FastAPI 실행 진입점
├── hearo_backend/                # v2 도메인·서비스·저장소·API·MQTT 브리지
├── tests/                        # v2 자동 테스트
├── docs/                         # API 명세와 배포 문서
├── infra/                        # AWS·Nginx·systemd·Mosquitto 구성 예시
├── legacy/                       # 이전 API 보존본(운영 사용 금지)
├── requirements.txt              # 표준 의존성 진입점
└── requirements-backend-v2.txt  # 배포에서 검증한 고정 버전
```

## 로컬 실행과 검증

Python 3.11~3.14를 지원합니다. EC2의 Python 3.14에서는 Python 3.14 wheel이 제공되는 Pydantic 2.13.4 이상이 필요하며, 이 프로젝트는 `2.13.4`로 고정합니다.

```bash
cd /path/to/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
python -m uvicorn dashboard_api_v2:app --reload --port 8001
```

개발 기본값은 메모리 저장소와 비활성 MQTT입니다. 서버를 재시작하면 개발 데이터가 사라집니다.

```bash
curl http://127.0.0.1:8001/health
```

Swagger UI는 `http://127.0.0.1:8001/docs`에서 확인합니다. 정상 기준은 전체 테스트 통과와 `{"status":"ok","version":"2.0.0"}` 응답입니다.

## 운영 데이터 주의사항

최종 User 레코드는 `login_id`, E.164 휴대폰 번호, `account_type`, nullable 가구 연동 상태를 사용합니다. 과거 이메일 기반 샘플 core 테이블과 구조가 다르므로 기존 테이블을 그대로 가리키지 마십시오. 먼저 별도의 최종 core 테이블에 배포하고 검증한 뒤 필요한 데이터만 명시적으로 이전합니다.

알림 테이블은 다음 키를 사용합니다.

```text
Partition key: household_id
Sort key: event_key = UTC timestamp#event_id
GSI: alarm_lookup_key = household_id#event_id
```

30일 이력은 KST 범위를 UTC로 변환해 DynamoDB `Query`로 읽고 `Scan`을 사용하지 않습니다.

## 보안 원칙

- 운영 환경은 HTTPS와 MQTT TLS 8883만 사용합니다.
- 사용자 JWT, device credential, MQTT 비밀번호는 서로 다른 인증 정보입니다.
- JWT·internal token은 각각 32자 이상의 독립 난수로 생성합니다.
- 긴급 주소, 전화번호, token, credential을 로그에 출력하지 않습니다.
- 초대 코드 미리보기·구성원 목록·알림·WebSocket에는 긴급 주소를 포함하지 않습니다.
- API는 단일 worker로 시작합니다. 다중 worker 전에는 WebSocket과 rate limit용 Redis가 필요합니다.

## 배포 상태와 최종 확인

- 기존 v1 `127.0.0.1:8000`을 유지하고 최종 v2를 `127.0.0.1:8001`에서 운영
- `hearo-core-v2-final`, `hearo-alerts-v2-final` 테이블 사용
- API와 MQTT 브리지를 systemd 자동 실행 서비스로 운영
- MQTT TLS 8883 연결과 가구별 수집 경로 검증
- 실제 프론트 도메인만 CORS에 등록
- `/v2/health`, 회원가입·로그인·주소·기기·30일 이력 smoke test 완료
- MQTT bridge가 새 내부 API에 상태·알림을 저장하는지 확인
- 모든 검증 후에만 프론트 base URL을 v2로 변경

운영 비밀번호, JWT secret, internal token, device credential, 인증서 개인키는 저장소에 커밋하지 않습니다. 상세 명령과 되돌리기 절차는 배포 가이드에 포함되어 있습니다.
