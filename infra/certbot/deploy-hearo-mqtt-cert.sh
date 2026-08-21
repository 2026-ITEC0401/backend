#!/usr/bin/env bash
set -euo pipefail

# Replace mqtt.example.com once during deployment. Certbot calls this hook for
# every renewed certificate; copying from the fixed MQTT lineage is harmless
# and ensures Mosquitto never needs access to Let's Encrypt's private folders.
MQTT_CERT_LINEAGE="${HEARO_MQTT_CERT_LINEAGE:-/etc/letsencrypt/live/mqtt.example.com}"
TARGET_DIR="/etc/mosquitto/certs"

for filename in fullchain.pem privkey.pem chain.pem; do
  if [[ ! -r "${MQTT_CERT_LINEAGE}/${filename}" ]]; then
    echo "MQTT 인증서 파일을 읽을 수 없습니다: ${MQTT_CERT_LINEAGE}/${filename}" >&2
    exit 1
  fi
done

install -d -o root -g mosquitto -m 0750 "${TARGET_DIR}"
install -o root -g mosquitto -m 0640 \
  "${MQTT_CERT_LINEAGE}/fullchain.pem" "${TARGET_DIR}/hearo-fullchain.pem"
install -o root -g mosquitto -m 0640 \
  "${MQTT_CERT_LINEAGE}/privkey.pem" "${TARGET_DIR}/hearo-privkey.pem"
install -o root -g mosquitto -m 0640 \
  "${MQTT_CERT_LINEAGE}/chain.pem" "${TARGET_DIR}/hearo-chain.pem"

systemctl try-reload-or-restart mosquitto.service
