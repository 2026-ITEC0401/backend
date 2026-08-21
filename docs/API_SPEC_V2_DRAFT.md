# Hearo 프론트엔드 연동용 백엔드 API 명세

- 문서 상태: 구현 완료·배포 전 최종 검증본
- 최초 작성일: 2026-08-17
- 구현 반영일: 2026-08-21

## 1. 서비스 규칙

- 로그인에는 `login_id`와 비밀번호를 사용합니다.
- 회원가입 필수 정보는 로그인 아이디, 이름, 휴대폰 번호, 비밀번호입니다.
- `신규 가구 등록` 사용자는 가구를 생성하고 `owner`가 됩니다.
- 신규 가구 등록 시 긴급 신고에 사용할 도로명주소를 필수로 등록하며, 주소는 사용자 프로필이 아닌 가구에 저장합니다.
- `가족·보호자로 참여` 사용자는 먼저 미연동 계정으로 가입하며, 초대 코드는 가입 직후 또는 설정에서 입력할 수 있습니다.
- 초대 코드는 6자리 영문·숫자 조합이며, 유효기간 동안 여러 가족이 같은 코드를 사용할 수 있습니다.
- 가족 구성원의 이름 편집은 실제 회원 이름을 변경하는 기능이 아니라, 현재 로그인한 사용자의 화면에만 적용되는 개인 표시 이름(alias) 설정입니다.
- 개인 표시 이름 설정은 owner 전용 기능이 아닙니다. 가구에 연동된 사용자라면 자신이 보는 각 구성원의 표시 이름을 설정·초기화할 수 있습니다.
- 가족 연동 해제는 다른 구성원을 가구에서 내보내는 기능이 아니라, 현재 로그인한 계정이 자신의 가구 연결을 끊는 기능입니다. owner가 다른 계정의 연결을 대신 해제할 수 없습니다.
- 일반 member가 연동을 해제하면 해당 member의 연결만 끊고 가구는 유지합니다. 가구 owner가 자신의 연동을 해제하면 소유권을 이전하지 않고 가구 전체를 비활성화합니다.
- 기기 설정은 MQTT 연결 ON/OFF와 LED 알림 ON/OFF만 제공합니다.
- 진동 알림과 민감도 조절은 화면과 API에서 제거합니다.
- 알림 이력은 한국시간 기준 오늘을 포함한 최근 30개 달력 날짜를 반환합니다.
- 녹음 파일을 생성·업로드·보관·재생하지 않습니다.

## 2. 공통 규칙

### 2.1 통신과 시간

- 운영 API는 HTTPS만 사용합니다.
- JSON 키는 `snake_case`를 사용합니다.
- 서버 저장 시각은 UTC ISO 8601 형식으로 반환합니다.
- 사용자 표시용 시각은 `Asia/Seoul` 기준 `local_time`을 함께 제공합니다.
- 가구 데이터 API는 access JWT가 필요합니다.
- 다른 가구 데이터에 접근하면 `403`을 반환합니다.
- 가구 미연동 사용자가 가구 전용 API를 호출하면 `409 HOUSEHOLD_LINK_REQUIRED`를 반환합니다.

### 2.2 인증 토큰

- access JWT 유효기간: 15분
- refresh JWT 유효기간: 30일
- refresh token은 사용할 때마다 회전하며 이전 token은 폐기합니다.
- 로그아웃 시 전달된 refresh token을 폐기합니다.

인증 헤더:

```text
Authorization: Bearer {access_token}
```

### 2.3 입력 규칙

| 필드 | 규칙 |
|---|---|
| `login_id` | 4~30자, 영문·숫자·마침표·밑줄·하이픈, 중복 불가 |
| `name` | 공백 제외 1~60자 |
| `phone_number` | 국내 번호 입력 허용, 서버에서 E.164로 정규화, 중복 불가 |
| `password` | 10자 이상, 영문자와 숫자 포함 |
| `invite_code` | 영문 대문자·숫자 6자리, 대소문자 구분 없이 처리 |
| `postal_code` | 숫자 5자리, 주소 검색 결과에서 선택 |
| `road_address` | 5~200자, 주소 검색 결과에서 선택, 줄바꿈·제어 문자 금지 |
| `detail_address` | 최대 200자, 사용자가 직접 입력, 줄바꿈·제어 문자 금지 |
| `address_provider` | `kakao_postcode` 또는 `juso_go_kr` |

회원가입 화면에는 별도 로그인 아이디 입력칸이 필요합니다. 현재 와이어프레임에 없다면 프론트 화면을 추가 수정해야 합니다.

### 2.4 공통 오류 형식

```json
{
  "code": "LOGIN_ID_ALREADY_EXISTS",
  "message": "이미 사용 중인 아이디입니다.",
  "field_errors": {
    "login_id": "다른 아이디를 입력해 주세요."
  },
  "request_id": "req-01H..."
}
```

| HTTP 상태 | 의미 |
|---:|---|
| `400` | 잘못된 요청 또는 만료·형식 오류 |
| `401` | 로그인 실패 또는 만료된 인증 |
| `403` | 다른 가구 접근 또는 권한 부족 |
| `404` | 대상 데이터 없음 |
| `409` | 아이디·휴대폰 중복, 가구 미연동, 상태 충돌 |
| `422` | 필수 입력 누락 또는 입력 검증 실패 |
| `429` | 로그인·초대 코드 조회 등의 요청 횟수 초과 |

## 3. 화면별 API 연결표

| 화면 | API | 비고 |
|---|---|---|
| 로그인·회원가입 선택 | 없음 | 화면 이동만 수행 |
| 로그인 | `POST /auth/login` | `login_id`, `password` |
| 가입 유형 선택 | 없음 | 선택값을 회원가입 요청에 포함 |
| 신규 가구 회원가입 | `POST /auth/signup` | 긴급 주소·사용자·가구·4개 기기 생성 |
| 가족 참여 회원가입 | `POST /auth/signup` | 미연동 사용자 생성 |
| 초대 코드 입력 | `POST /households/link/preview` | 가구 정보를 조회만 함 |
| 조회 가구 연동 | `POST /households/link` | 확인 후 실제 연동 |
| 나중에 하기 | 없음 | 미연동 상태 유지 |
| 설정 메인 | `GET /me`, `GET /households/current` | 사용자와 연동 상태 표시 |
| 긴급 주소 조회·수정 | `GET/PATCH /households/{id}/emergency-address` | 연동 사용자 조회, owner 수정 |
| 가족 표시 이름 변경 | `PATCH /households/{id}/members/{member_user_id}/display-name` | 호출자 화면에만 적용되는 별칭 |
| 가족 표시 이름 초기화 | `DELETE /households/{id}/members/{member_user_id}/display-name` | 가입 시 이름으로 복원 |
| 비밀번호 변경 | `PATCH /me/password` | 기존 비밀번호 확인 |
| owner 가족 설정 | `GET /households/{id}/invite-code` | 현재 공유 코드 표시 |
| 초대 코드 재발급 | `POST /households/{id}/invite-code/rotate` | 기존 코드 즉시 폐기 |
| 미연동 가족 설정 | `POST /households/link/preview`, `POST /households/link` | 가입 이후에도 연동 가능 |
| 연동 가족 목록 | `GET /households/{id}/members` | 이름·휴대폰·역할 표시 |
| 내 계정의 가구 연동 해제 | `DELETE /households/current/link` | member는 본인 연결 해제, owner는 가구 비활성화 |
| 메인 기기 목록 | `GET /households/{id}/devices` | 고정 4개 기기와 상태 표시 |
| 기기 연결 설정 | `PATCH /households/{id}/devices/{device_id}/connection` | owner만 가능 |
| LED 알림 설정 | `PATCH /households/{id}/devices/{device_id}/settings` | owner만 가능 |
| 메인 최신 알림 | `GET /households/{id}/alarms/latest` | 가장 최근 알림 1건 |
| 알림 날짜별 목록 | `GET /households/{id}/alarms/history` | 최근 30일 전체 |
| 알림 상세 보기 | `GET /households/{id}/alarms/{alarm_id}` | 선택한 알림 1건 |
| 실시간 상태·알림 | `WS /ws/households/{id}` | 기기 상태와 신규 알림 |

## 4. 인증·회원가입 API

### 4.1 회원가입

```text
POST /auth/signup
```

신규 가구 등록 요청:

```json
{
  "login_id": "hearo_user01",
  "name": "홍길동",
  "phone_number": "010-1234-5678",
  "password": "StrongPassword123",
  "signup_type": "new_household",
  "household_name": "홍길동 가구",
  "emergency_address": {
    "postal_code": "41566",
    "road_address": "대구광역시 북구 대학로 80",
    "detail_address": "101동 902호",
    "address_provider": "kakao_postcode"
  },
  "terms_service_agreed": true,
  "privacy_agreed": true
}
```

가족·보호자 참여 요청:

```json
{
  "login_id": "family_user01",
  "name": "김가족",
  "phone_number": "010-9876-5432",
  "password": "StrongPassword123",
  "signup_type": "family_member",
  "terms_service_agreed": true,
  "privacy_agreed": true
}
```

- `emergency_address`는 `new_household` 가입에만 필수입니다.
- `family_member` 가입에는 주소를 입력하지 않고, 가구 연동 후 해당 가구 주소를 사용합니다.
- `updated_at`은 클라이언트가 전송하지 않고 서버가 저장 시 생성합니다.

응답:

```json
{
  "user": {
    "user_id": "user-a1b2",
    "login_id": "family_user01",
    "name": "김가족",
    "phone_number": "+821098765432",
    "account_type": "family_member",
    "household_id": null,
    "role": null,
    "household_link_status": "unlinked"
  },
  "tokens": {
    "access_token": "...",
    "refresh_token": "...",
    "token_type": "bearer",
    "expires_in": 900
  }
}
```

신규 가구 가입 응답에는 최초 기기 설치용 credential이 한 번만 추가로 반환됩니다. 이 값은 프론트에서 지속 보관하지 않고 설치 담당자에게 안전하게 전달해야 합니다.

### 4.2 로그인

```text
POST /auth/login
```

```json
{
  "login_id": "hearo_user01",
  "password": "StrongPassword123"
}
```

성공 응답에는 사용자 정보와 access·refresh token을 반환합니다. 아이디 또는 비밀번호가 틀린 경우 어느 항목이 틀렸는지 구분하지 않고 동일한 `401 INVALID_CREDENTIALS`를 반환합니다.

### 4.3 토큰 회전과 로그아웃

```text
POST /auth/refresh
POST /auth/logout
```

```json
{
  "refresh_token": "..."
}
```

### 4.4 내 정보 조회

```text
GET /me
```

회원가입 때 입력한 실제 이름은 사용자 프로필의 `name`으로 유지합니다. 가족 설정 화면의 이름 편집은 이 값을 변경하지 않으며, 5.6절의 사용자별 표시 이름 API를 사용합니다.

### 4.5 로그인 상태의 비밀번호 변경

```text
PATCH /me/password
```

```json
{
  "current_password": "StrongPassword123",
  "new_password": "NewStrongPassword456"
}
```

변경 성공 시 기존 access·refresh token을 모두 무효화하고 재로그인을 요구합니다.
로그인하지 못한 사용자의 비밀번호 찾기는 SMS 인증 수단이 확정되기 전까지 이번 명세에서 제외합니다.

## 5. 가구·가족 연동 API

### 5.1 현재 가구 조회

```text
GET /households/current
```

연동된 사용자:

```json
{
  "household_link_status": "linked",
  "household": {
    "household_id": "home-a1b2c3",
    "name": "홍길동 가구",
    "member_count": 3,
    "created_at": "2026-08-17T03:00:00Z"
  },
  "membership": {
    "role": "member",
    "linked_at": "2026-08-17T04:00:00Z"
  }
}
```

미연동 사용자:

```json
{
  "household_link_status": "unlinked",
  "household": null,
  "membership": null
}
```

### 5.2 초대 코드 조회·재발급

```text
GET  /households/{household_id}/invite-code
POST /households/{household_id}/invite-code/rotate
```

- owner만 사용할 수 있습니다.
- 코드는 6자리 영문 대문자·숫자 조합입니다.
- 초대 코드는 24시간 유효합니다.
- 유효기간 안에는 여러 가족 계정이 같은 코드를 사용할 수 있습니다.
- 재발급하면 기존 코드는 즉시 사용할 수 없게 됩니다.
- 서버에는 원문이 아닌 코드 해시를 저장합니다.

```json
{
  "invite_code": "A7K2M9",
  "expires_at": "2026-08-18T03:00:00Z"
}
```

`코드 공유하기`는 프론트의 OS 공유 기능을 사용하며 별도 백엔드 API가 필요하지 않습니다.

### 5.3 초대 코드로 가구 미리보기

```text
POST /households/link/preview
```

```json
{
  "invite_code": "A7K2M9"
}
```

```json
{
  "linkable": true,
  "household": {
    "name": "홍길동 가구",
    "member_count": 3,
    "created_at": "2026-08-17T03:00:00Z"
  }
}
```

- 가구 주소와 구성원 개인정보는 미리보기에서 반환하지 않습니다.
- 없는 코드, 만료 코드, 형식 오류는 구분 가능한 오류 코드로 반환하되 모두 요청 횟수 제한을 적용합니다.

### 5.4 가구 연동

```text
POST /households/link
```

```json
{
  "invite_code": "A7K2M9"
}
```

성공 응답:

```json
{
  "household_link_status": "linked",
  "household_id": "home-a1b2c3",
  "role": "member",
  "linked_at": "2026-08-17T04:00:00Z"
}
```

이미 가구에 연동된 사용자는 다른 가구에 동시에 연동할 수 없습니다.

### 5.5 가족 구성원 목록

```text
GET /households/{household_id}/members
```

```json
{
  "members": [
    {
      "user_id": "user-owner",
      "profile_name": "홍길동",
      "display_name": "아버지",
      "display_name_is_custom": true,
      "phone_number": "+821012345678",
      "role": "owner",
      "linked_at": "2026-08-17T03:00:00Z",
      "is_me": false,
      "can_edit_display_name": true
    },
    {
      "user_id": "user-member",
      "profile_name": "김가족",
      "display_name": "김가족",
      "display_name_is_custom": false,
      "phone_number": "+821098765432",
      "role": "member",
      "linked_at": "2026-08-17T04:00:00Z",
      "is_me": true,
      "can_edit_display_name": true
    }
  ]
}
```

연동 가족 전화·문자 버튼은 이 응답의 전화번호에 `tel:` 또는 `sms:`를 붙여 프론트에서 실행합니다. 서버가 전화나 문자를 대신 발송하지 않습니다.

`display_name`은 현재 API를 호출한 사용자에게 적용된 값입니다. 별칭이 없으면 `profile_name`과 같은 값을 반환합니다. 같은 구성원을 보더라도 사용자마다 서로 다른 `display_name`을 받을 수 있습니다.

### 5.6 가족 구성원 개인 표시 이름 설정

```text
PATCH  /households/{household_id}/members/{member_user_id}/display-name
DELETE /households/{household_id}/members/{member_user_id}/display-name
```

설정 요청:

```json
{
  "display_name": "우리 엄마"
}
```

- owner와 member 모두 사용할 수 있습니다.
- 대상 구성원의 실제 `profile_name`은 변경하지 않습니다.
- 설정값은 요청한 사용자에게만 보입니다. 다른 가족 계정의 화면에는 영향을 주지 않습니다.
- `DELETE`는 개인 표시 이름을 지우고 가입 시 이름을 다시 표시합니다.
- 저장 키는 `viewer_user_id + member_user_id` 조합으로 분리하여 다른 사용자의 별칭을 덮어쓰지 않도록 합니다.

### 5.7 내 계정의 가족 연동 해제

```text
DELETE /households/current/link
```

- owner 권한을 요구하지 않으며, 현재 로그인한 사용자가 자신의 연동만 해제할 수 있습니다.
- 다른 구성원의 계정·멤버십·표시 이름을 삭제하거나 변경하지 않습니다.
- owner를 포함해 다른 사용자의 연동을 대신 해제하는 API는 제공하지 않습니다.
- 일반 member가 호출하면 해당 계정만 `unlinked` 상태로 돌아가며 가구는 계속 활성 상태를 유지합니다.
- owner가 호출하면 소유권을 다른 구성원에게 이전하지 않고 가구의 `status`를 `inactive`로 변경합니다. 가구에 연결된 모든 계정은 더 이상 해당 가구의 기기·알림 API를 사용할 수 없습니다.
- owner의 가구 비활성화가 완료되면 `emergency_address`는 즉시 삭제합니다. 가구 자체를 영구 삭제하는 경우에도 별도 주소 레코드를 남기지 않습니다.
- 가구 비활성화는 사용자 계정이나 기존 알림 데이터를 즉시 삭제하는 작업이 아닙니다.
- 프론트는 owner에게 일반 연동 해제와 영향 범위가 다르다는 확인 안내를 표시해야 합니다.

### 5.8 긴급 신고 주소 조회·수정

```text
GET   /households/{household_id}/emergency-address
PATCH /households/{household_id}/emergency-address
```

- 주소는 사용자 프로필이 아닌 가구에 저장합니다.
- 가구에 연동된 owner와 member는 `GET`으로 조회할 수 있습니다.
- `PATCH`는 owner만 사용할 수 있습니다.
- 미연동 사용자와 다른 가구 사용자는 조회할 수 없습니다.
- 초대 코드 미리보기, 구성원 목록, 일반 알림, WebSocket 및 서버 로그에는 주소를 포함하지 않습니다.
- 도로명주소와 우편번호는 프론트 주소 검색 결과를 사용하고 상세주소만 직접 입력합니다.

응답 및 수정 요청 본문:

```json
{
  "postal_code": "41566",
  "road_address": "대구광역시 북구 대학로 80",
  "detail_address": "101동 902호",
  "address_provider": "kakao_postcode",
  "updated_at": "2026-08-20T04:00:00Z"
}
```

`PATCH` 요청에는 서버 생성 필드인 `updated_at`을 제외합니다. 기존 가구에 주소가 등록되지 않았다면 `GET`은 `404`를 반환하고 owner가 `PATCH`로 최초 등록할 수 있습니다.

## 6. 기기 API

고정 기기:

| 기기 ID | 위치 |
|---|---|
| `rpi-001` | 거실 |
| `esp32_1` | 안방 |
| `esp32_2` | 현관 |
| `esp32_3` | 화장실 |

### 6.1 기기 목록

```text
GET /households/{household_id}/devices
```

```json
{
  "devices": [
    {
      "device_id": "esp32_2",
      "location": "현관",
      "desired_mqtt_connected": true,
      "reported_mqtt_connected": true,
      "network_status": "online",
      "ui_status": "connected",
      "last_seen_at": "2026-08-17T05:00:00Z",
      "led_alert_enabled": true,
      "audio_streaming": true,
      "microphone_ok": true,
      "config_version": 7
    }
  ]
}
```

기기 상태:

| 상태 | 의미 |
|---|---|
| `connected` | MQTT 연결 정상 |
| `disabled_by_owner` | owner가 MQTT 연결을 끔 |
| `pending` | 최신 설정 반영 대기 |
| `offline` | 네트워크 offline 또는 heartbeat 45초 초과 |
| `error` | 요청 후 제한 시간 안에 최신 설정 미반영 |

### 6.2 MQTT 연결 ON/OFF

```text
PATCH /households/{household_id}/devices/{device_id}/connection
```

```json
{
  "enabled": false
}
```

- owner만 변경할 수 있습니다.
- 이 스위치는 전원이 아니라 MQTT 연결을 제어합니다.
- OFF 상태에서도 기기 전원과 Wi-Fi는 유지됩니다.
- Raspberry Pi는 MQTT OFF 중에도 AI 추론을 계속합니다.

### 6.3 LED 알림 ON/OFF

```text
PATCH /households/{household_id}/devices/{device_id}/settings
```

```json
{
  "led_alert_enabled": false
}
```

진동과 민감도 필드는 요청·응답에 포함하지 않습니다.

## 7. 알림 API

### 7.1 최근 알림

```text
GET /households/{household_id}/alarms/latest
```

알림이 없으면 `{"alarm": null}`을 반환합니다.

### 7.2 최근 30일 날짜별 알림

```text
GET /households/{household_id}/alarms/history
```

- `Asia/Seoul` 기준 오늘을 포함한 최근 30개 달력 날짜입니다.
- 알림이 없는 날짜도 `alarms: []`로 포함합니다.
- 날짜는 최신순, 날짜 안 알림도 최신순입니다.
- 페이지네이션 없이 30일 범위를 한 번에 반환합니다.

```json
{
  "timezone": "Asia/Seoul",
  "start_date": "2026-07-19",
  "end_date": "2026-08-17",
  "total_count": 2,
  "days": [
    {
      "date": "2026-08-17",
      "display_label": "오늘",
      "alarms": [
        {
          "id": "alert-001",
          "time": "2026-08-17T00:15:00Z",
          "local_time": "2026-08-17T09:15:00+09:00",
          "location": "현관",
          "source_device_id": "esp32_2",
          "sound": "도어락소리",
          "raw_label": "도어락_개방음",
          "type": "Visitor",
          "confidence": 0.91
        }
      ]
    },
    {
      "date": "2026-08-16",
      "display_label": "어제",
      "alarms": []
    }
  ]
}
```

알림 객체에는 `audio_url`, 녹음 재생 정보, 세탁 완료 이벤트를 포함하지 않습니다. `112 전화·문자`와 가족 연락은 프론트가 `tel:`·`sms:`로 실행합니다.

`알림 오류`와 `이상 없음` 피드백 저장 기능은 요구사항이 확정되지 않아 이번 명세에서 제외합니다. 버튼을 유지하려면 별도 피드백 API 합의가 필요합니다.

### 7.3 특정 알림 상세 조회

```text
GET /households/{household_id}/alarms/{alarm_id}
```

- 해당 가구에 연동된 owner와 member만 조회할 수 있습니다.
- 다른 가구 사용자는 `403`, 존재하지 않는 알림은 `404`를 반환합니다.
- `alarm_id`는 최근 30일 목록의 알림 객체에 포함된 `id`를 사용합니다.
- 프론트는 응답의 `local_time`을 한국어 오전·오후와 시각으로 표시합니다.

```json
{
  "alarm": {
    "id": "alert-001",
    "date": "2026-07-08",
    "local_time": "2026-07-08T08:47:00+09:00",
    "location": "안방",
    "sound": "비상벨소리",
    "type": "Urgent"
  }
}
```

상세 응답에는 화면에 필요한 위 필드만 포함합니다. `source_device_id`, `raw_label`, `confidence` 및 AI 진단 필드는 사용자용 상세 API에 노출하지 않습니다.

## 8. WebSocket

```text
WS /ws/households/{household_id}
```

연결 후 첫 메시지:

```json
{
  "type": "auth",
  "access_token": "..."
}
```

기기 상태 변경:

```json
{
  "type": "device.status_changed",
  "device_id": "esp32_2",
  "ui_status": "disabled_by_owner",
  "last_seen_at": "2026-08-17T05:00:03Z",
  "config_version": 8
}
```

새 알림:

```json
{
  "type": "alarm.created",
  "alarm": {
    "id": "alert-001",
    "sound": "도어락소리",
    "location": "현관",
    "time": "2026-08-17T00:15:00Z"
  }
}
```

프론트는 60초보다 짧은 간격으로 `ping`을 보내고 `pong`을 받아 연결을 유지합니다. access token 갱신 후 WebSocket도 새 token으로 다시 연결합니다.

## 9. 프론트엔드 수정 확인 목록

- 모든 회원가입 화면에 별도 로그인 아이디 입력칸을 추가합니다.
- 기기 상세에서 진동 알림과 민감도 조절을 삭제합니다.
- 알림 상세에서 녹음 재생 UI를 삭제합니다.
- 구성원 이름 편집은 사용자별 표시 이름 변경으로 연결하며, 실제 회원 이름을 바꾸지 않습니다.
- 가구 연동 해제 화면은 member에게는 본인 연결만 해제된다고 안내하고, owner에게는 가구 전체가 비활성화된다는 별도 확인 안내를 표시합니다.
- 신규 가구 회원가입에 도로명주소 검색, 우편번호 및 상세주소 입력 UI를 추가합니다.
- 가족·보호자 참여 회원가입에서는 주소를 입력받지 않습니다.
- 설정과 긴급 신고 화면은 전용 긴급 주소 API를 사용하며 다른 일반 응답에서 주소를 찾지 않습니다.

## 10. 배포 전 확인 사항

1. 초대 코드 유효기간은 24시간으로 구현했습니다.
2. 로그인하지 못한 사용자의 비밀번호 찾기는 SMS 본인 인증 정책 확정 전까지 제외합니다.
3. 운영 EC2에는 현재 실행 중인 v1과 병렬로 v2를 배포한 뒤 프론트 smoke test 후 전환합니다.
4. 과거 이메일 기반 샘플 core 테이블은 최종 User 모델과 호환되지 않으므로 별도 최종 테이블을 사용합니다.
