# Hearo 키트 등록 및 가구 귀속 API 명세 — v2.5.0

**상태: v2.5.0 구현 계약. 기존 v2.4.0에는 미구현·미배포였으며, 새 소스의 OpenAPI에는 아래 3개 경로가 포함됩니다.** 소스 구현과 운영 배포는 별개입니다. 외부 `/v2/health`의 `2.5.0`, 새 경로 및 실제 등록 검증을 확인한 뒤 운영 기능으로 사용합니다. [배포 안내](DEPLOYMENT_DEVICE_KIT_V2_5.md)와 [기존 백엔드 API 명세](API_SPEC_V2_DRAFT.md)를 함께 참고합니다.

owner는 기존 방식으로 가입하고, 생성된 가구에 키트 ID와 등록 코드를 입력하여 Raspberry Pi 1대와 ESP32 3대를 등록합니다. 키트 등록 성공은 서버에 소유 가구가 기록됐다는 의미입니다. 실제 장비 설정은 설치 담당자가 진행하며, 기기의 연결 상태는 기존 heartbeat와 MQTT 상태로 확인합니다.

## 적용 범위

- 가구당 키트 1개를 등록합니다. 키트는 Raspberry Pi 1대와 ESP32 3대로 구성합니다.
- 기존 회원가입 요청과 응답을 유지합니다. owner 가입 시 논리적 기기 4대 및 기기별 credential을 계속 생성합니다.
- 회원가입 후 별도 화면에서 키트를 등록합니다. 키트 미등록이나 등록 실패로 이미 생성된 계정과 가구를 삭제하지 않습니다.
- owner는 미리보기와 등록을 수행하고, member는 가구의 등록 상태를 조회합니다.
- 기존 가구는 `legacy_registered`로 취급합니다. 키트 번호가 없어도 기존 기기 기능을 계속 이용합니다.
- Wi-Fi 설정, credential 배포, MQTT 계정과 ACL 설정, 펌웨어 업로드는 설치 담당자가 기존 절차로 수행합니다.
- 사용자용 키트 등록 해제, 교체, 양도 및 공장 초기화 API는 이번 범위에 포함하지 않습니다.

## 기기 식별과 상태

`kit_id`는 제품 한 세트의 고유 번호이며 `hardware_id`는 각 물리 보드의 고유 번호입니다. `device_id`는 가구 안에서 사용하는 기존 역할 ID입니다. 공개된 제품 번호는 인증정보가 아니며, 등록 권한은 별도 등록 코드로 확인합니다.

| 가구 내 역할 ID | 종류 | 기본 위치 | 물리 기기 ID 예시 |
|---|---|---|---|
| `rpi-001` | `hub` | 거실 | `HR-RPI-0001` |
| `esp32_1` | `alert_node` | 안방 | `HR-ESP-0001` |
| `esp32_2` | `alert_node` | 현관 | `HR-ESP-0002` |
| `esp32_3` | `alert_node` | 화장실 | `HR-ESP-0003` |

이 명세의 새 API는 위 순서로 기기 4대를 반환합니다. 기존 `/devices` 응답 배열의 순서는 변경하지 않으므로 프론트는 항상 `device_id`로 기기를 대응시킵니다.

### 키트 등록 상태

| `status` | 의미 | 화면 표시 | `kit_id`와 `claimed_at` | `can_claim` |
|---|---|---|---|---|
| `unregistered` | 신규 가구이며 키트를 등록하지 않음 | 키트 미등록 | `null` | owner만 `true` |
| `claimed` | 키트가 해당 가구에 등록됨 | 키트 등록 완료 | 실제 등록 값 | `false` |
| `legacy_registered` | 기능 도입 이전의 기존 가구 | 등록 완료 | `null` | `false` |

`claimed`를 실제 기기 연결 완료로 해석하지 않습니다. `hardware_id`는 등록된 물리 기기의 표시 정보이며 기기 인증에 사용하지 않습니다.

### 기기 연결 상태

기존 `ui_status`의 값과 계산을 유지합니다.

| `ui_status` | 화면 표시 |
|---|---|
| `connected` | 연결됨 |
| `disabled_by_owner` | 사용자가 비활성화함 |
| `pending` | 설정 적용 중 |
| `offline` | 오프라인 |
| `error` | 설정 적용 실패 |

키트 등록 API는 기기를 강제로 `connected`나 `offline`으로 바꾸지 않습니다. 새 응답의 `devices[].ui_status`는 기존 `/devices`와 같은 계산으로 만든 조회 시점의 상태입니다. 이후 연결 상태 갱신은 기존 기기 API와 WebSocket을 사용합니다.

## 공통 요청 규칙

운영 환경의 기존 base URL은 `https://13.233.91.248/v2`입니다. 아래 경로는 이 base URL 뒤에 붙입니다. 예를 들어 등록 상태 조회의 외부 URL은 `https://13.233.91.248/v2/households/{household_id}/device-kit`입니다. 신규 API 배포 전에는 이 경로가 존재하지 않습니다.

```http
Authorization: Bearer {access_token}
Accept: application/json
```

POST에는 다음 헤더도 필요합니다.

```http
Content-Type: application/json
```

`household_id`는 현재 로그인 사용자의 가구 ID를 사용합니다. 클라이언트가 `owner_user_id`, `hardware_id` 또는 등록 시각을 요청 본문에 보내지 않습니다. 서버는 로그인 사용자와 사전 등록된 키트에서 이 정보를 결정합니다.

시각은 UTC ISO 8601 문자열로 반환합니다. 성공 응답은 JSON이며 서버가 보낸 `X-Request-ID`를 오류 문의에 사용할 수 있습니다. 새 API의 성공·오류 응답에는 `Cache-Control: no-store`를 적용합니다.

### 등록 입력

두 POST API가 동일한 요청 본문을 사용합니다.

```json
{
  "kit_id": "HEARO-KIT-0001",
  "claim_code": "K7QM-29XA"
}
```

| 필드 | 필수 | 규칙 |
|---|---|---|
| `kit_id` | 예 | 앞뒤 공백 제거와 영문 대문자 변환 후 `^HEARO-KIT-[A-Z0-9]{4,32}$` 일치 |
| `claim_code` | 예 | 앞뒤 공백 제거와 영문 대문자 변환 후 `^[A-Z0-9]{4}-[A-Z0-9]{4}$` 일치 |

등록 코드는 영문 대문자와 숫자 8자를 가운데 ASCII 하이픈으로 구분합니다. 앞뒤 공백을 제거한 입력이 ASCII인지 확인한 뒤 대문자로 변환합니다. 내부 공백, 전각 문자, 유니코드 하이픈은 허용하지 않습니다. 입력 길이는 정규화 전에도 각각 최대 64자와 32자로 제한합니다. 두 필드 이외의 본문 필드는 `422 VALIDATION_ERROR`입니다.

## API 목록과 권한

| 기능 | 메서드와 경로 | 권한 | DB 변경 |
|---|---|---|---|
| 가구 키트 등록 상태 조회 | `GET /households/{household_id}/device-kit` | 연결된 owner와 member | 없음 |
| 입력한 키트 확인 | `POST /households/{household_id}/device-kit/claim/preview` | 해당 가구 owner | 없음 |
| 키트 등록 확정 | `POST /households/{household_id}/device-kit/claim` | 해당 가구 owner | 가구와 기기 4대에 귀속 정보 저장 |

## 가구 키트 등록 상태 조회

```http
GET /households/{household_id}/device-kit
```

요청 본문과 쿼리 파라미터는 없습니다. 앱 시작, 로그인 계정 변경, 기기 관리 화면 진입 및 등록 결과 복구 시 호출합니다.

### 신규 가구 응답

```json
{
  "household_id": "home-example0001",
  "status": "unregistered",
  "kit_id": null,
  "claimed_at": null,
  "can_claim": true,
  "devices": [
    {"device_id": "rpi-001", "device_type": "hub", "location": "거실", "hardware_id": null, "ui_status": "offline", "last_seen_at": null},
    {"device_id": "esp32_1", "device_type": "alert_node", "location": "안방", "hardware_id": null, "ui_status": "offline", "last_seen_at": null},
    {"device_id": "esp32_2", "device_type": "alert_node", "location": "현관", "hardware_id": null, "ui_status": "offline", "last_seen_at": null},
    {"device_id": "esp32_3", "device_type": "alert_node", "location": "화장실", "hardware_id": null, "ui_status": "offline", "last_seen_at": null}
  ]
}
```

### 등록된 가구 응답

```json
{
  "household_id": "home-example0001",
  "status": "claimed",
  "kit_id": "HEARO-KIT-0001",
  "claimed_at": "2026-10-06T03:00:00Z",
  "can_claim": false,
  "devices": [
    {"device_id": "rpi-001", "device_type": "hub", "location": "거실", "hardware_id": "HR-RPI-0001", "ui_status": "offline", "last_seen_at": null},
    {"device_id": "esp32_1", "device_type": "alert_node", "location": "안방", "hardware_id": "HR-ESP-0001", "ui_status": "offline", "last_seen_at": null},
    {"device_id": "esp32_2", "device_type": "alert_node", "location": "현관", "hardware_id": "HR-ESP-0002", "ui_status": "offline", "last_seen_at": null},
    {"device_id": "esp32_3", "device_type": "alert_node", "location": "화장실", "hardware_id": "HR-ESP-0003", "ui_status": "offline", "last_seen_at": null}
  ]
}
```

HTTP 상태는 `200`입니다. 등록 직후 기기 4대가 모두 `offline`이어도 정상입니다. 이미 heartbeat가 있는 논리 기기는 실제 계산 결과를 반환합니다.

기존 가구는 같은 응답 구조에서 `status="legacy_registered"`, `kit_id=null`, `claimed_at=null`, `can_claim=false`를 반환합니다. 기존에 물리 번호를 보관하지 않은 기기의 `hardware_id`는 `null`이며, `ui_status`와 `last_seen_at`은 현재 상태를 반환합니다. 기존 가구의 기기를 다시 미등록으로 표시하지 않습니다.

### 공통 응답 필드

등록 확정 API도 동일한 응답 구조를 사용합니다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `household_id` | string | 현재 접근한 가구 |
| `status` | enum | `unregistered`, `claimed`, `legacy_registered` |
| `kit_id` | string 또는 null | 등록된 제품 키트 번호 |
| `claimed_at` | string 또는 null | 최초 등록 성공 시각 |
| `can_claim` | boolean | 현재 사용자에게 등록 UI를 제공할 수 있는지 나타내는 값 |
| `devices` | array | 논리적 기기 4대 |
| `devices[].device_id` | string | 기존 가구 내 역할 ID |
| `devices[].device_type` | enum | `hub` 또는 `alert_node` |
| `devices[].location` | string | 해당 가구의 기존 설치 위치 |
| `devices[].hardware_id` | string 또는 null | 등록된 물리 기기 번호 |
| `devices[].ui_status` | enum | 기존 기기 연결 상태 |
| `devices[].last_seen_at` | string 또는 null | 기존 서버 기기 상태에 기록된 마지막 수신 시각 |

member가 미등록 가구를 조회하면 `status="unregistered"`이지만 `can_claim=false`입니다. 권한은 서버에서 항상 재검증하며 `can_claim`만으로 API 접근을 허용하지 않습니다.

## 입력한 키트 확인

```http
POST /households/{household_id}/device-kit/claim/preview
```

공통 등록 입력을 본문으로 보냅니다. 존재 여부와 등록 코드를 확인한 뒤 키트에 포함된 기기 정보를 제공합니다.

```json
{
  "claimable": true,
  "kit_id": "HEARO-KIT-0001",
  "devices": [
    {"device_id": "rpi-001", "device_type": "hub", "location": "거실", "hardware_id": "HR-RPI-0001"},
    {"device_id": "esp32_1", "device_type": "alert_node", "location": "안방", "hardware_id": "HR-ESP-0001"},
    {"device_id": "esp32_2", "device_type": "alert_node", "location": "현관", "hardware_id": "HR-ESP-0002"},
    {"device_id": "esp32_3", "device_type": "alert_node", "location": "화장실", "hardware_id": "HR-ESP-0003"}
  ]
}
```

HTTP 상태는 `200`입니다. 성공 시 `claimable`은 항상 `true`이며, 등록할 수 없으면 공통 오류 응답을 반환합니다. `location`은 해당 가구의 기존 논리 기기 위치입니다. 이 API는 키트의 종류와 역할을 대조하고 임의로 위치를 변경하지 않습니다.

미리보기는 등록이나 예약이 아닙니다. 사용자가 확인 화면을 보는 동안 다른 가구가 등록하면 확정 요청이 `409 KIT_ALREADY_CLAIMED`로 실패할 수 있습니다. 등록 코드와 권한을 확정 요청에서 다시 확인합니다.

이미 키트가 있는 가구와 기존 등록 가구의 미리보기는 `409 HOUSEHOLD_ALREADY_HAS_KIT`입니다. 같은 키트의 등록 결과를 다시 확인할 때는 GET을 사용합니다.

## 키트 등록 확정

```http
POST /households/{household_id}/device-kit/claim
```

공통 등록 입력을 본문으로 보냅니다. 미리보기를 먼저 호출하지 않았어도 서버가 전체 검증을 수행합니다.

최초 성공과 동일 등록 요청의 재전송은 모두 `200`을 반환합니다. 응답 구조는 등록 상태 조회의 등록된 가구 응답과 같으며 `status="claimed"`입니다.

서버는 키트, 가구, 기기 4대의 귀속 정보를 하나의 트랜잭션으로 저장합니다. 실패하면 일부 기기만 등록된 상태가 남아서는 안 됩니다. 기존 credential, MQTT 설정, LED 설정, 연결 희망값 및 `config_version`은 이 등록으로 변경하지 않습니다.

### 요청 재전송

- 같은 가구와 같은 키트에 올바른 등록 코드로 다시 요청하면 현재 등록 결과를 반환합니다.
- 최초 `claimed_at`과 물리 기기 연결 정보를 다시 생성하거나 변경하지 않습니다.
- 잘못된 등록 코드는 재요청에서도 `400 INVALID_KIT_CREDENTIALS`이며 성공 처리하지 않습니다.
- 다른 가구에 등록된 키트는 `409 KIT_ALREADY_CLAIMED`입니다.
- 같은 가구에 다른 키트를 등록하려 하면 `409 HOUSEHOLD_ALREADY_HAS_KIT`입니다.
- 별도 `Idempotency-Key` 헤더는 사용하지 않습니다.

등록 코드에는 이번 범위에서 시간에 따른 자동 만료를 적용하지 않습니다. 최초 등록 뒤에는 다른 가구의 등록에 사용할 수 없으며, 동일 가구의 동일 키트 재요청에만 사용할 수 있습니다. 이를 위해 코드 해시는 등록 후에도 유지합니다. 코드 유출 시 교체·폐기는 설치 담당자의 별도 관리 절차로 처리합니다.

POST 응답을 받기 전에 통신이 끊기면 먼저 GET으로 서버 상태를 조회합니다. 같은 `kit_id`가 이미 등록돼 있으면 완료 화면을 표시합니다. 여전히 `unregistered`이면 사용자가 재시도하도록 안내합니다. 상태를 조회할 수 없는 상황에서 등록 실패를 확정하거나 가입을 다시 진행하지 않습니다.

## 오류 응답

기존 오류 형식을 유지합니다.

```json
{
  "code": "INVALID_KIT_CREDENTIALS",
  "message": "키트 ID 또는 등록 코드를 확인해 주세요.",
  "field_errors": {},
  "request_id": "req-example"
}
```

| HTTP | `code` | 의미와 프론트 처리 |
|---|---|---|
| 400 | `INVALID_KIT_CREDENTIALS` | 존재하지 않는 키트 또는 등록 코드 불일치. 두 입력을 함께 확인하도록 안내 |
| 401 | `AUTHENTICATION_REQUIRED` | 인증 헤더 없음. 로그인 안내 |
| 401 | `INVALID_ACCESS_TOKEN` | 토큰 오류 또는 만료. 기존 refresh 처리 후 재시도 |
| 401 | `REVOKED_ACCESS_TOKEN` | 폐기된 토큰. 로그인 상태 정리 |
| 403 | `HOUSEHOLD_ACCESS_DENIED` | 다른 가구 접근 |
| 403 | `OWNER_REQUIRED` | member의 미리보기 또는 등록 요청 |
| 409 | `HOUSEHOLD_LINK_REQUIRED` | 가구 미연동 상태 |
| 409 | `HOUSEHOLD_INACTIVE` | 가구 비활성화. 현재 가구 상태 재조회 |
| 409 | `KIT_ALREADY_CLAIMED` | 다른 가구가 사용 중인 키트 |
| 409 | `HOUSEHOLD_ALREADY_HAS_KIT` | 미리보기: 이미 키트가 있거나 기존 등록 가구임. 확정: 다른 키트가 있거나 기존 등록 가구임. 동일 키트의 유효 재요청은 `200` |
| 409 | `KIT_CONFIGURATION_INVALID` | 사전 등록된 기기 구성 또는 귀속 정보가 불완전함. 설치 담당자 문의 |
| 409 | `KIT_CLAIM_CONFLICT` | 등록 중 가구 또는 기기 상태가 변경됨. GET 조회 후 상태에 맞게 안내 |
| 422 | `VALIDATION_ERROR` | 필수값 누락, 형식 오류 또는 허용하지 않은 필드. `field_errors` 표시 |
| 429 | `KIT_CLAIM_RATE_LIMITED` | 입력 요청 제한. 반복 자동 요청 중단 후 잠시 뒤 시도 안내 |
| 503 | `KIT_SERVICE_UNAVAILABLE` | 저장소 등의 일시적 장애. GET으로 성공 여부 확인 후 재시도 안내 |

입력이 형식에는 맞지만 키트가 없거나 코드가 다르면 동일한 `400` 응답을 제공합니다. 등록 번호 존재 여부를 코드 검증 없이 노출하지 않습니다. 서버 오류에는 등록 코드, credential, 다른 가구 ID 또는 내부 예외 문자열을 포함하지 않습니다.

서버는 기존 인증·가구 접근 권한을 먼저 검사합니다. 형식이 유효한 POST는 공유 입력 제한을 적용한 뒤 키트와 코드를 확인하고, 유효한 코드가 확인된 경우에만 해당 키트의 타 가구 귀속 여부나 구성 오류를 알려줍니다. 요청 가구 자체가 이미 등록됐다는 정보는 현재 가구 조회 권한으로 확인할 수 있으므로, 미리보기에서 먼저 `HOUSEHOLD_ALREADY_HAS_KIT`를 반환할 수 있습니다.

### 입력 횟수 제한

미리보기와 등록 확정은 하나의 제한을 공유합니다. 로그인 사용자 기준 15분에 10회, 접속 IP 기준 15분에 30회를 허용하며 둘 중 하나를 넘으면 `429`입니다. owner 권한과 본문 형식 검증을 통과한 호출은 등록 성공 여부와 관계없이 합산합니다. 앞 단계의 인증·권한 오류와 `422` 형식 오류는 이 제한에 합산하지 않습니다. 키트 번호를 바꾸어도 사용자와 IP 제한이 초기화되지 않습니다. GET 등록 상태 조회는 이 입력 제한에 포함하지 않습니다.

현재 운영의 단일 API worker 구성에 맞춰 적용합니다. 여러 worker 또는 여러 서버로 확장할 때는 공유 저장소 기반 제한을 적용합니다.

## 프론트 처리 순서

1. 기존 owner 회원가입과 주소 온보딩을 진행합니다. `/households/current`의 주소 온보딩 계약을 유지합니다.
2. 주소 온보딩 완료 후 또는 기기 관리 화면 진입 시 GET 키트 상태를 조회합니다.
3. `unregistered`와 `can_claim=true`이면 키트 ID와 등록 코드 입력 화면을 보여줍니다.
4. member의 `unregistered`에는 owner가 등록해야 한다는 안내를 표시합니다.
5. 미리보기 POST 성공 시 키트 ID와 기기 4대를 보여주고 등록 확인을 받습니다.
6. 등록 확정 POST 성공 시 응답으로 화면 상태를 갱신하고 등록 코드를 메모리에서 제거합니다.
7. 등록 완료 화면에는 “키트 등록이 완료되었습니다. 기기 설정이 완료되면 연결 상태를 확인할 수 있습니다.”를 표시합니다.
8. `claimed`와 `legacy_registered`에서는 입력 화면을 숨기고 기존 기기 연결 상태를 표시합니다.
9. 이후 기기 상태는 기존 `/devices`, `connection.ready`, `device.status_changed`, `device.config_changed`로 갱신합니다. 역할 ID로 대응시키며 등록 메타데이터를 WebSocket 데이터로 덮어쓰지 않습니다.
10. 앱 재실행, 계정 변경, 기기 관리 화면 재진입 및 등록 응답 유실 시 GET으로 상태를 복구합니다.

키트 미등록은 기존 `ui_status` 값을 바꾸는 대신 화면 안내에 적용합니다. 현재 연결된 논리 기기가 있더라도 키트 등록 상태와 기기 연결 상태는 별도로 취급합니다. 이번 등록은 실제 보드에 설정을 자동 배포하지 않으므로, 특정 물리 보드와 논리 역할의 일치는 설치 담당자가 확인합니다.

키트 상태 변경용 신규 WebSocket 이벤트는 추가하지 않습니다. 다른 사용자나 화면에서 등록 결과를 확인할 때 GET을 사용합니다. 지속적인 짧은 간격의 상태 조회는 요구하지 않습니다.

등록 코드는 입력 화면과 확인 요청에만 일시적으로 사용하며 성공·화면 이탈·취소·로그아웃 시 메모리에서 제거합니다. 입력란은 기본 마스킹하고 사용자가 선택할 때만 일시적으로 표시할 수 있습니다. 등록 코드, device credential, MQTT 비밀번호, 오디오 PSK를 URL, 브라우저 로그, 분석 도구, 오류 수집 도구의 요청 본문 또는 `localStorage`에 보관하지 않습니다. 새 API 응답에는 credential, 등록 코드 및 코드 해시를 포함하지 않습니다.

## 백엔드 구현 조건

### 신규와 기존 가구 구분

신규 owner 가입 시 가구 레코드에 `device_kit_status="unregistered"`를 명시 저장합니다. 기존 가구 레코드에 필드가 없으면 읽기 시 `legacy_registered`로 해석합니다. 판단에 프로세스 시작 시각이나 날짜 비교를 사용하지 않습니다.

등록 트랜잭션은 가구의 상태 필드가 명시적으로 `unregistered`인 경우만 허용합니다. 필드가 없는 기존 가구까지 등록을 허용하는 조건은 사용하지 않습니다. 기존 `home-b0d7a380b457`는 재등록을 요구하지 않습니다.

### 사전 등록 데이터

관리자가 발급한 키트만 등록할 수 있습니다. 기존 core 테이블의 `pk`와 `sk` 아래에 키트와 물리 기기 번호를 저장할 수 있어 새 테이블이나 GSI가 필수는 아닙니다.

| 데이터 | `pk` | `sk` | 주요 내용 |
|---|---|---|---|
| 키트 기본정보 | `KIT#{kit_id}` | `META` | 코드 해시, 재고 상태, 구성 버전, 귀속 가구, 등록 시각 |
| 키트의 기기 구성 | `KIT#{kit_id}` | `DEVICE#{device_id}` | 물리 번호, 논리 역할, 기기 종류 |
| 물리 번호 중복 방지 | `HARDWARE#{hardware_id}` | `KIT` | 키트 ID와 논리 역할 |
| 기존 가구 | `HOUSE#{household_id}` | `META` | 키트 등록 상태, 키트 ID, 최초 등록 시각 |
| 기존 기기 | `HOUSE#{household_id}` | `DEVICE#{device_id}` | 기존 필드에 키트 ID와 물리 번호 추가 |

키트 재고 상태는 `unclaimed`와 `claimed`로 구분하고, API의 가구 상태 `unregistered`와 혼용하지 않습니다. 키트는 고정 역할 4개가 정확히 한 번씩 있으며 물리 번호도 모두 달라야 합니다. 재고 생성 시 전역 물리 번호 중복을 조건부 저장으로 차단합니다. 등록 가능 재고의 구성은 발급 후 임의로 변경하지 않고 구성 버전을 유지합니다.

등록 코드는 암호학적 난수로 발급하고 기존 Argon2id 라이브러리로 해시하여 보관합니다. 기존 비밀번호의 길이 정책과 다른 9자 코드이므로 `hash_password()`를 그대로 호출하지 않고 코드용 검증·해시 함수를 분리합니다. 코드 원문을 DB에 저장하거나 서버 로그에 출력하지 않습니다. 예시의 코드와 제품 번호는 실제 등록 데이터가 아닙니다. 키트와 물리 번호 귀속 레코드에는 TTL을 설정하지 않습니다.

### 등록 확정과 데이터 보존

- 가구가 활성 상태이고 요청 사용자가 여전히 owner이며, 키트를 등록하지 않은 상태인지 확인합니다.
- 키트 코드, `unclaimed` 상태 및 읽은 구성 버전을 검증합니다.
- 기존 기기 4대가 모두 존재하고 역할과 종류가 맞으며 다른 키트에 연결되지 않았는지 검증합니다.
- 키트 기본정보 1개, 가구 기본정보 1개, 기존 기기 4개 및 물리 번호 중복 방지 레코드 4개를 하나의 조건부 DynamoDB 트랜잭션으로 갱신합니다.
- 물리 번호 중복 방지 레코드도 해당 키트와 논리 역할에 속하며 다른 가구에 귀속되지 않았는지 확인합니다. 재고 발급 시 중복 방지와 등록 시 귀속 검증을 모두 적용합니다.
- 같은 레코드에 `ConditionCheck`와 `Update`를 각각 넣지 않고, 각 갱신 작업에 조건을 함께 넣습니다. 가구 갱신 조건에는 활성 상태와 owner 일치를 포함하여 등록 중 연동 해제와의 경쟁도 차단합니다.
- 기존 기기에는 전체 `Put` 대신 `kit_id`와 `hardware_id`만 부분 갱신합니다. 메모리 저장소도 하나의 잠금 안에서 전체 사전검증 후 일괄 반영하여 같은 원자성을 보장합니다.
- 등록한 사용자 ID는 새 필드로 저장하지 않습니다. 요청 시 현재 가구의 owner를 검증하고, 귀속 기록에는 가구 ID와 등록 시각만 저장합니다.
- 등록 상태 GET, 등록 성공 응답 및 조건 충돌 후 재판정에는 `ConsistentRead=True` 조회를 사용합니다. 여러 레코드의 조회는 하나의 원자적 스냅샷이 아니므로 가구와 기기 4대의 매핑이 일치하는지도 확인하며, 동시 변경으로 다르면 다시 조회합니다. 정합성을 확인할 수 없으면 `503`을 반환하고 임의의 미등록 또는 완료 상태를 반환하지 않습니다.
- 트랜잭션 조건 충돌 후에는 키트·가구·기기·물리 번호의 최신 상태를 다시 읽어 동일 가구와 동일 키트의 성공 요청인지 판단합니다. 권한과 코드가 유효하고 기기 4대의 매핑까지 일치할 때만 멱등 성공을 반환합니다.
- 모든 저장소 오류를 무조건 `409`로 처리하지 않습니다. 일시적인 저장소 장애는 `503`이며 서버가 처리 결과를 확인해야 합니다.
- 키트 등록은 기존 `DEVICECRED` 레코드, MQTT 계정, topic, ACL, LED, 오디오 통계, 상태 보고 및 설정 버전을 변경하지 않습니다.

### 탈퇴와 가구 비활성화

member의 연동 해제와 탈퇴는 키트 귀속에 영향을 주지 않습니다. 현재 v2.4.0의 파기 로직대로 owner의 연동 해제 또는 탈퇴 시 가구·기기 등록정보는 삭제되고 남은 구성원은 미연동이 됩니다. 키트 재고와 물리 번호 귀속은 `claimed`로 유지하고 자동으로 `unclaimed`로 돌리지 않습니다. 사용자가 삭제됐다는 이유로 다른 가구가 키트를 가져갈 수 없어야 합니다.

이번 기능은 별도 등록자 사용자 ID를 저장하지 않습니다. 기존 owner의 연동 해제·탈퇴 흐름과 90일 알림 보관 정책을 유지합니다. 키트 재고에 남는 가구 ID는 재등록 방지용 귀속 정보이며 계정·이름·전화번호·주소·기기 credential을 보관하지 않습니다. 관리자 재설정은 장비, 기존 인증정보 및 MQTT 권한을 확인하는 별도 절차로 진행합니다.

## 배포 전 완료 기준

| 검증 | 기대 결과 |
|---|---|
| 새 owner 가입 | 기존 기기와 credential 4대 생성 유지, 새 GET은 `unregistered` |
| 기존 운영 가구 | `legacy_registered`, 기존 기기 연결과 LED 기능 정상 |
| member와 다른 가구 접근 | 조회 권한과 owner 전용 변경 권한 정확히 적용 |
| 미리보기 | DB, credential, MQTT, 설정 버전에 변경 없음 |
| 정상 등록 | 키트와 가구 및 기기 4대가 함께 귀속, 비밀값 응답 없음 |
| 동일 요청 재전송 | `200`, 최초 시각과 매핑 보존 |
| 두 가구의 동시 등록 | 하나만 성공, 다른 요청은 충돌 응답 |
| 두 키트의 동일 가구 동시 등록 | 한 키트만 귀속 |
| 서로 다른 키트의 동일 물리 번호 사용 | 재고 발급과 등록 단계 모두 중복 차단 |
| 구성 오류와 트랜잭션 실패 | 부분 귀속 없음 |
| 기기 회귀 | 기존 credential로 config와 heartbeat 정상, LED와 MQTT 및 알림 정상 |
| owner 탈퇴와 연동 해제 | 가구 비활성화 유지, 신규 등록자 참조 없음, 키트 자동 해제 없음 |
| member 탈퇴 | 키트 귀속과 다른 사용자의 기기 기능 유지 |
| 입력 제한 | preview와 claim 합산 제한, 번호 변경으로 우회 불가 |
| 프론트 응답 유실 | GET으로 상태 복구, 가입이나 등록의 중복 처리 없음 |
