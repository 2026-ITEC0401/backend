# Hearo 백엔드 v2 배포 및 롤백

이 문서는 현재 검증한 EC2 병렬 배포 구조에 v2.3.0 변경을 반영하는 절차를 기준으로 합니다.

```text
Nginx HTTPS /v2/
        ↓
FastAPI v2 127.0.0.1:8001
        ↓
DynamoDB hearo-core-v2-final / hearo-alerts-v2-final

Mosquitto TLS 8883
        ↓
hearo-mqtt-bridge-v2-final
        ↓
FastAPI 내부 MQTT 수집 API
```

## 1. 원칙

- 기존 v1의 `8000` 포트를 유지한 채 v2를 `8001`에 배포합니다.
- Uvicorn과 DynamoDB 포트는 외부에 직접 공개하지 않습니다.
- API, MQTT 브리지, 기기는 서로 다른 인증정보를 사용합니다.
- 실제 `.env`, 비밀번호, token, credential, 개인키를 Git에 저장하지 않습니다.
- 운영 전환 전 테스트와 rollback 경로를 확보합니다.

## 2. 코드와 의존성

v2.3.0 신규 릴리스 디렉터리는 `/opt/hearo-backend-v2-r5`를 사용합니다. 운영 전환 전까지 현재 v2.2.0 r4 디렉터리를 유지합니다.

```bash
cd /opt/hearo-backend-v2-r5
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

정상 기준은 모든 테스트 통과와 다음 import 결과입니다.

```bash
.venv/bin/python -c 'import dashboard_api_v2; print(dashboard_api_v2.app.title, dashboard_api_v2.app.version)'
```

```text
Hearo API 2.3.0
```

## 3. DynamoDB

`infra/aws-v2.yaml`은 다음 리소스를 정의합니다.

- `hearo-core-v2-final`: 사용자·가구·기기·연락처·토큰
- `hearo-alerts-v2-final`: 가구별 알림과 `alarm-lookup-index`
- EC2 애플리케이션 역할의 최소 DynamoDB 권한

운영 역할에는 트랜잭션을 위한 `dynamodb:TransactWriteItems`와 `dynamodb:ConditionCheckItem`이 모두 필요합니다. v2.3.0 회원 탈퇴는 해당 사용자의 만료 전 refresh token과 개인 표시 이름 키만 찾기 위해 core 테이블의 `dynamodb:Scan`도 사용합니다. 쓰기는 기존 `DeleteItem` 권한만 사용합니다.

## 4. 환경변수

예시 파일을 복사한 뒤 EC2에서 실제 값을 채웁니다.

```bash
sudo install -d -o root -g root -m 755 /etc/hearo
sudo install -o root -g root -m 600 infra/hearo-api.env.example /etc/hearo/hearo-api-v2.env
sudo install -o root -g root -m 600 infra/hearo-mqtt-bridge.env.example /etc/hearo/hearo-mqtt-bridge-v2.env
```

두 파일의 `HEARO_INTERNAL_TOKEN`은 같아야 합니다. JWT secret, internal token, API MQTT 비밀번호, 브리지 MQTT 비밀번호는 각각 별도로 생성합니다.

운영 CORS는 실제 HTTPS 프론트 주소를 사용합니다. 로컬 Vite 개발을 위해서는 정확히 `http://localhost:5173`만 HTTP 예외로 추가할 수 있으며, 다른 HTTP 호스트나 포트는 운영 검증에서 거부됩니다.

API 환경 파일에는 행안부 **검색 API** 승인키, 별도로 발급받은 **상세주소 API** 승인키와 제한 시간을 추가합니다. 승인키는 Git이나 프론트 환경변수에 저장하지 않습니다.

```dotenv
HEARO_JUSO_CONFIRM_KEY=행안부_검색_API_승인키
HEARO_JUSO_DETAIL_CONFIRM_KEY=행안부_상세주소_API_승인키
HEARO_JUSO_TIMEOUT_SECONDS=5
HEARO_ALARM_UNREAD_BASELINE_AT=2026-08-30T00:00:00Z
```

`HEARO_ALARM_UNREAD_BASELINE_AT`은 v2.2.0 운영 전환 직전 `date -u`로 확인한 UTC 시각을 ISO 8601 `Z` 형식으로 한 번만 기록합니다. 기존 구성원의 최초 미확인 계산 기준이므로 서비스 재시작이나 재배포 때 임의로 변경하지 않습니다. 신규 owner/member는 각각 가구 생성·연동 시각을 별도로 저장합니다.

## 5. systemd

```bash
sudo install -o root -g root -m 644 infra/systemd/hearo-api-v2-final.service /etc/systemd/system/hearo-api-v2-final.service
sudo install -o root -g root -m 644 infra/systemd/hearo-mqtt-bridge-v2-final.service /etc/systemd/system/hearo-mqtt-bridge-v2-final.service
sudo systemd-analyze verify /etc/systemd/system/hearo-api-v2-final.service /etc/systemd/system/hearo-mqtt-bridge-v2-final.service
sudo systemctl daemon-reload
```

기존 테스트 API가 `8001`을 사용한다면 먼저 중지한 뒤 최종 API를 시작합니다.

```bash
sudo systemctl stop hearo-api-v2-test
sudo systemctl start hearo-api-v2-final
curl http://127.0.0.1:8001/health
```

정상 확인 뒤 자동 시작을 설정합니다.

```bash
sudo systemctl enable hearo-api-v2-final
sudo systemctl disable hearo-api-v2-test
sudo systemctl enable --now hearo-mqtt-bridge-v2-final
```

## 6. Smoke test

다음을 순서대로 확인합니다.

1. `/v2/health`와 `/v2/docs`가 `200`을 반환합니다.
2. 신규 owner 회원가입이 `201`을 반환합니다.
3. `/me`, `/households/current`, 주소 온보딩과 기기 목록이 정상 응답을 반환합니다.
4. 고정 기기 `rpi-001`, `esp32_1`, `esp32_2`, `esp32_3`가 생성됩니다.
5. 행안부 도로명·상세주소 검색과 owner 주소 등록, member 수정 `403`을 확인합니다.
6. 최근 7일 이력은 데이터가 없어도 7개 날짜를 반환합니다.
7. MQTT 브리지가 `MQTT bridge subscribed to household-scoped topics`를 기록합니다.
8. 실제 알림을 발행한 뒤 최신·이력·상세조회 응답이 일치하고 상세 응답에 `raw_label`이 있습니다.
9. WebSocket `alarm.created`의 `alarm` 객체에 `type`과 `raw_label`이 있습니다.
10. 기존 owner의 첫 `GET .../alarms/unread-count`가 배포 이전 알림을 0건으로 처리합니다.
11. 배포 후 새 알림을 생성하면 owner와 member의 미확인 개수가 각각 증가합니다.
12. owner가 `PATCH .../alarms/seen`을 호출하면 owner만 0이 되고 member 개수는 유지됩니다.
13. ESP32 설정 응답의 `led_alert_control_supported`는 `true`, Raspberry Pi는 `false`이고 Pi LED 변경 요청은 `409 DEVICE_LED_CONTROL_UNSUPPORTED`입니다.
14. 별도로 만든 삭제 전용 테스트 계정에서만 `DELETE /me`를 실행하여 `204`와 재로그인 `401`을 확인합니다. 실제 운영 계정으로 smoke test하지 않습니다.

회원가입 응답의 device credential은 한 번만 원문으로 반환되므로 안전하게 장비별로 전달합니다.

## 7. 롤백

최종 API가 시작되지 않거나 health check에 실패하면 최종 서비스를 먼저 중지하고 백업한 환경파일과 systemd 설정을 복원한 뒤 기존 r4 코드를 다시 시작합니다. v2.3.0은 새 DynamoDB 필드를 추가하지 않으므로 API 코드·systemd 작업 경로와 IAM 정책을 이전 상태로 복원하면 됩니다.

```bash
sudo systemctl stop hearo-mqtt-bridge-v2-final
sudo systemctl stop hearo-api-v2-final
sudo systemctl start hearo-api-v2-test
curl http://127.0.0.1:8001/health
```

기존 v1 `8000`, 기존 DynamoDB 테이블과 릴리스 압축파일은 v2 검증이 끝날 때까지 삭제하지 않습니다.
