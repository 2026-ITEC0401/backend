# 운영 인프라 예시

이 폴더는 Hearo 백엔드 v2 운영에 필요한 선언적 구성과 예시 파일을 보관합니다.

| 경로 | 역할 |
|---|---|
| `aws-v2.yaml` | DynamoDB 두 테이블과 EC2 애플리케이션 역할 예시 |
| `hearo-api.env.example` | FastAPI 운영 환경변수 형식 |
| `hearo-mqtt-bridge.env.example` | MQTT 브리지 운영 환경변수 형식 |
| `nginx/` | HTTPS `/v2/` 프록시 설정 |
| `systemd/` | API와 MQTT 브리지 자동 실행 서비스 |
| `mosquitto/` | TLS listener와 가구별 ACL 생성 예시 |
| `certbot/` | 인증서 갱신 뒤 Mosquitto 인증서를 반영하는 예시 |

`.env`, MQTT 비밀번호, JWT secret, internal token, device credential 및 개인키는 저장소에 추가하지 않습니다. 실제 값은 EC2의 `/etc/hearo/`와 Mosquitto 전용 경로에 제한된 권한으로 저장합니다.
