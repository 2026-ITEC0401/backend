# Hearo 백엔드 v2 배포 및 롤백

이 문서는 현재 검증한 EC2 병렬 배포 구조에 v2.1.1 변경을 반영하는 절차를 기준으로 합니다.

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

검증된 릴리스 디렉터리는 `/opt/hearo-backend-v2-r2`입니다.

```bash
cd /opt/hearo-backend-v2-r2
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
Hearo API 2.1.1
```

## 3. DynamoDB

`infra/aws-v2.yaml`은 다음 리소스를 정의합니다.

- `hearo-core-v2-final`: 사용자·가구·기기·연락처·토큰
- `hearo-alerts-v2-final`: 가구별 알림과 `alarm-lookup-index`
- EC2 애플리케이션 역할의 최소 DynamoDB 권한

운영 역할에는 트랜잭션을 위한 `dynamodb:TransactWriteItems`와 `dynamodb:ConditionCheckItem`이 모두 필요합니다.

## 4. 환경변수

예시 파일을 복사한 뒤 EC2에서 실제 값을 채웁니다.

```bash
sudo install -d -o root -g root -m 755 /etc/hearo
sudo install -o root -g root -m 600 infra/hearo-api.env.example /etc/hearo/hearo-api-v2.env
sudo install -o root -g root -m 600 infra/hearo-mqtt-bridge.env.example /etc/hearo/hearo-mqtt-bridge-v2.env
```

두 파일의 `HEARO_INTERNAL_TOKEN`은 같아야 합니다. JWT secret, internal token, API MQTT 비밀번호, 브리지 MQTT 비밀번호는 각각 별도로 생성합니다.

API 환경 파일에는 행안부 **검색 API** 승인키, 별도로 발급받은 **상세주소 API** 승인키와 제한 시간을 추가합니다. 승인키는 Git이나 프론트 환경변수에 저장하지 않습니다.

```dotenv
HEARO_JUSO_CONFIRM_KEY=행안부_검색_API_승인키
HEARO_JUSO_DETAIL_CONFIRM_KEY=행안부_상세주소_API_승인키
HEARO_JUSO_TIMEOUT_SECONDS=5
```

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

회원가입 응답의 device credential은 한 번만 원문으로 반환되므로 안전하게 장비별로 전달합니다.

## 7. 롤백

최종 API가 시작되지 않거나 health check에 실패하면 최종 서비스를 먼저 중지하고 기존 테스트 API를 복구합니다.

```bash
sudo systemctl stop hearo-mqtt-bridge-v2-final
sudo systemctl stop hearo-api-v2-final
sudo systemctl start hearo-api-v2-test
curl http://127.0.0.1:8001/health
```

기존 v1 `8000`, 기존 DynamoDB 테이블과 릴리스 압축파일은 v2 검증이 끝날 때까지 삭제하지 않습니다.
