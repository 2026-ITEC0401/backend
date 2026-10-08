# 키트 등록 v2.5.0 배포·시연 안내

## 범위와 현재 상태

새 API 3개는 [키트 명세](DEVICE_KIT_API_SPEC.md)를 따릅니다. 기존 v2.4.0 API 계약, 회원 탈퇴, MQTT Bridge, ESP32 LED 및 알림 90일 정책을 유지합니다. 기존 운영은 r6이며 새 API/Bridge만 **r7**로 전환합니다. **r6 소스와 알림 정리 서비스·타이머·요약 어댑터는 변경하지 않습니다.** 기존 데모 가구는 `legacy_registered`이며 재등록을 요구하지 않습니다. 기능 배포 이후 가입한 신규 owner 가구만 `unregistered`로 시작합니다.

소스·로컬/Moto 시험 완료는 실제 AWS나 운영 배포 완료가 아닙니다. GitHub CI → EC2 검증 → 외부 버전/경로 → 실제 프론트 등록으로 완료 여부를 각각 확인합니다. 큰 4MiB 합성 시험은 재실행하지 않습니다. 새 테이블·GSI·TTL·IAM 변경이나 펌웨어 재업로드는 이번 기능의 필수 절차가 아닙니다. 기존 역할에 현재 core 테이블의 `GetItem/Query/PutItem/UpdateItem/ConditionCheckItem` 및 격리 합성 정리용 `DeleteItem/Scan` 권한이 있어야 합니다. 트랜잭션은 포함된 각 작업의 권한으로 검사됩니다([AWS 설명](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)). 권한 부족 시 광범위 권한을 임의로 추가하지 않습니다.

## 1. GitHub 병합 후 새 체크아웃 준비

검증 완료된 main 커밋을 `kit_release_sha`에 입력합니다. 자동으로 최신 커밋에 운영을 맞추지 않습니다. 기존 r6 폴더에서 pull하지 마십시오.

```bash
kit_release_sha=검증된_40자리_커밋_SHA
bash -Eeuo pipefail <<'BASH'
test ! -e /opt/hearo-backend-v2-r7
sudo install -d -o ubuntu -g ubuntu -m 755 /opt/hearo-backend-v2-r7
git clone https://github.com/2026-ITEC0401/backend.git /opt/hearo-backend-v2-r7
BASH
git -C /opt/hearo-backend-v2-r7 checkout --detach "$kit_release_sha"
cd /opt/hearo-backend-v2-r7
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install 'moto[dynamodb]==5.2.3'
.venv/bin/python -m compileall -q hearo_backend scripts tests
bash -n scripts/deploy_v250_cutover.sh
env HEARO_ENV=test HEARO_STORE=memory HEARO_MQTT_ENABLED=false \
  AWS_EC2_METADATA_DISABLED=true HEARO_TERMS_VERSION= HEARO_PRIVACY_VERSION= \
  HEARO_LEGAL_EFFECTIVE_AT= HEARO_TERMS_URL= HEARO_PRIVACY_URL= \
  HEARO_LEGAL_CONSENT_REQUIRED=false .venv/bin/python -B -E -m pytest -q
```

위 테스트는 외부 AWS를 호출하지 않습니다. 테스트 폴더·새 소스는 r7에만 생성되며 r6 운영이 계속 동작합니다. 이미 r7 폴더가 있으면 삭제하지 말고 상태부터 확인합니다.

## 2. 작은 격리 DynamoDB API 시험

기존 격리 테이블 `hearo-core-v2-r6-staging`과 `hearo-alerts-v2-r6-staging`, 환경파일 `/etc/hearo/hearo-api-v2-r6-ddb-validation.env`를 재사용합니다. 운영 테이블·MQTT 연결은 거부합니다. 기존 localhost:8003 서비스의 코드를 교체하지 않으며 새 r7의 ASGI API handler와 실제 DynamoDB 저장소를 시험합니다.

```bash
cd /opt/hearo-backend-v2-r7
kit_release_sha=$(git rev-parse HEAD)
kit_test_target=377152274782/ap-south-1/hearo-core-v2-r6-staging/hearo-alerts-v2-r6-staging
install -d -m 700 /home/ubuntu/.config/hearo/kit-tests
sudo systemd-run --wait --pipe --collect \
  --property=User=ubuntu --property=Group=ubuntu \
  --property=EnvironmentFile=/etc/hearo/hearo-api-v2-r6-ddb-validation.env \
  --property=WorkingDirectory=/opt/hearo-backend-v2-r7 \
  /opt/hearo-backend-v2-r7/.venv/bin/python -B -E -u \
  scripts/verify_device_kit_ddb.py \
  --confirm-sha "$kit_release_sha" --confirm-target "$kit_test_target"
```

`KIT DDB READ-ONLY PREFLIGHT OK`는 계정·키트 생성 없는 읽기 전용 확인입니다. 확인 후 한 번만 실행합니다. 복구 파일은 **기존 파일이 없는 새 이름**을 사용합니다.

```bash
sudo systemd-run --wait --pipe --collect \
  --property=User=ubuntu --property=Group=ubuntu \
  --property=EnvironmentFile=/etc/hearo/hearo-api-v2-r6-ddb-validation.env \
  --property=WorkingDirectory=/opt/hearo-backend-v2-r7 \
  /opt/hearo-backend-v2-r7/.venv/bin/python -B -E -u \
  scripts/verify_device_kit_ddb.py --apply \
  --confirm-sha "$kit_release_sha" --confirm-target "$kit_test_target" \
  --recovery-file /home/ubuntu/.config/hearo/kit-tests/v250-first-run.json
```

성공 기준 `KIT REAL DYNAMODB API SMOKE OK`: 임시 owner 가입 → 미등록 조회 → preview 무변경 → claim → 같은 요청 재전송 → 틀린 코드 차단 → 기존 credential 유지 → owner 탈퇴 → 이번 합성 재고 9행만 조건부 삭제 → 시험 전 core/alerts 내용 일치. 오류 시 자동 재시도·자동 정리하지 않습니다. 비공개 복구 파일과 현재 DB부터 확인합니다. 이 시험은 실기기·MQTT broker·프론트·자연 용량 경계 검증이 아닙니다.

## 3. 운영 전환 — 사용자 실행

운영 환경값과 배포 커밋을 다시 점검하고 root700 백업에 r6 소스·기존 환경파일·서비스 2개를 저장합니다. env는 바꾸지 않고 서비스 파일의 실행 경로만 r7로 전환합니다. 종료 순서는 Bridge → API, 시작 순서는 API 정상 확인 → Bridge입니다. 구·신 API writer를 동시에 실행하지 않습니다.

```bash
cd /opt/hearo-backend-v2-r7
kit_release_sha=$(git rev-parse HEAD)
sudo bash scripts/deploy_v250_cutover.sh --check-only "$kit_release_sha"
```

읽기 전용 사전 확인 후, 테스트 성공을 확인한 경우에만:

```bash
sudo bash scripts/deploy_v250_cutover.sh --apply "$kit_release_sha" --staging-verified
```

`V2.5 SERVICE CUTOVER OK`는 API/Bridge active·r7 경로·부팅 enabled·외부 health2.5.0·3개 OpenAPI 경로까지 확인한 결과입니다. 실제 등록 성공을 대신하지 않습니다. 기존 r7 스테이징이 실행 중이면 먼저 그 서비스만 중단합니다. 운영 로그는 비밀값을 출력하지 않는 방식으로 확인합니다.

```bash
systemctl show hearo-api-v2-final hearo-mqtt-bridge-v2-final \
  -p Id -p ActiveState -p MainPID -p NRestarts -p WorkingDirectory --no-pager
curl --connect-timeout 10 --max-time 20 -fsS https://13.233.91.248/v2/health
sudo journalctl -u hearo-api-v2-final -u hearo-mqtt-bridge-v2-final \
  --since '5 minutes ago' --no-pager -o cat
systemctl is-active hearo-alert-retention.timer
```

사전점검 오류는 기존 r6 서비스를 유지합니다. 전환이 시작된 뒤 오류나 중단 신호가 발생하면 스크립트가 Bridge → API 중단을 요청합니다. 중단 실패 가능성이 있으므로 출력된 서비스 상태도 확인해야 합니다. 구버전 서비스와 DB는 자동 복원하지 않습니다.

전환 중 오류가 나면 같은 apply를 재실행하거나 구버전을 즉시 재시작하지 않습니다. 일부 API 요청이 이미 저장됐을 수 있으므로 새 소스 또는 설정을 수정하는 방식으로 복구합니다. 특히 새 가구를 old r6 signup으로 만든 뒤 새로운 키트 상태를 기대하거나 기존 `unregistered/claimed` 상태를 임의로 초기화하지 않습니다. r6 백업 복원은 서비스/DB 변경 내역을 확인한 뒤 별도 승인·절차로 진행합니다. 최신 회원 탈퇴·90일 필터를 제거하는 v2.3.0 롤백은 하지 않습니다.

## 4. 프론트 시험용 또는 실제 키트 발급

키트 ID는 제품 세트 번호, hardware ID는 물리 보드 표시번호이며 현재의 역할 ID(`rpi-001`, `esp32_1` 등)와 다릅니다. 아래 번호는 **시험용 표시번호**이고 물리 기기를 자동 설정하지 않습니다. 실제 제품은 합의한 고유번호로 대체합니다. 기본 미리보기는 AWS 호출·코드 생성·DB 변경이 없습니다.

```bash
install -d -m 700 /home/ubuntu/.config/hearo/device-kits
cd /opt/hearo-backend-v2-r7
.venv/bin/python -B -E scripts/provision_device_kit.py \
  --kit-id HEARO-KIT-DEMO20261008 \
  --rpi-hardware-id HR-RPI-DEMO20261008 \
  --esp32-1-hardware-id HR-ESP-DEMO20261008-1 \
  --esp32-2-hardware-id HR-ESP-DEMO20261008-2 \
  --esp32-3-hardware-id HR-ESP-DEMO20261008-3 \
  --region ap-south-1 --core-table hearo-core-v2-final \
  --output-file /home/ubuntu/.config/hearo/device-kits/demo20261008-kit-registration-code.json
```

위 입력·대상을 확인한 후 같은 명령에 `--apply --confirm-target 377152274782/ap-south-1/hearo-core-v2-final`을 추가합니다. 예상 성공 메시지는 `kit_inventory_created`입니다. 원문 등록 코드는 새600 파일에만 저장하며 DB에는 Argon2id 해시만 저장합니다. 파일을 GitHub·공개 로그·단체 채팅에 올리지 않습니다. 발급 결과가 불명확하면 **새 코드를 생성해서 재시도하지 않고** 해당 파일과 서버 재고부터 확인합니다. 9개 재고 항목은 모두 신규 키로 조건부 생성되며, 같은 hardware ID 재사용은 거부됩니다. 원래 데모 기기 인증정보를 덮어쓰지 않습니다.

## 5. 내일 프론트 확인 순서

1. 새 임시 owner 가입(`age_over_14_agreed: true`)과 주소 온보딩. 기존 데모 owner는 탈퇴하지 않습니다.
2. GET이 `unregistered/can_claim=true`, member는 `can_claim=false`인지 확인.
3. 발급된 키트 ID·코드로 preview4대 → 등록 → `claimed` 확인.
4. 페이지 재진입 후 등록 결과 유지, 같은 요청은200, 틀린 코드는400.
5. 다른 신규 가구에 같은 키트 등록은409. 기존 데모 가구는 `legacy_registered`로 기기 화면 유지.
6. 등록 직후 기기가 offline인 것은 실제 보드 설정 전이면 정상. 장비의 가구 credential·MQTT 계정/ACL·Wi-Fi는 기존 설치 절차로 따로 적용해야 합니다.
7. 탈퇴 시연은 별도 임시 계정에서 수행. owner 탈퇴는 가구·기기 인증을 삭제하므로 등록 시험 계정에만 사용합니다. 사용한 키트는 자동 해제되지 않습니다.

## 검증·남은 범위

문법/import, API/Memory, Moto 원자성·조건 경합, 기존 전체 회귀, 발급 도구 비공개 파일, 배포 스크립트 문법을 각각 점검합니다. 실제 AWS 결과와 EC2 적용은 사용자 출력으로 확인합니다. 공모전 데모의 물리 MQTT/UDP/LED/소리 분류는 장비 사용 가능 시 별도 확인하며, 이 릴리스가 통신 안정성을 보장한다고 가정하지 않습니다. 감사기록 분리·일반 로그90일 변경, 키트 양도/교체/재설정 및 장비 자동 provisioning은 추가하지 않습니다.
