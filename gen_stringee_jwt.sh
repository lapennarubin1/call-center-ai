#!/bin/bash
API_SID="AC50a1aa324d52698728ae498e5d34386a"
API_SECRET="SEWgD8mlmbshvT87T7F7Czdd5k8JdWdp"
NOW=$(date +%s)
EXP=$((NOW + 43200))

HEADER=$(echo -n '{"cty":"stringee-api;v=1","typ":"JWT","alg":"HS256"}' | base64 -w 0 | tr '+/' '-_' | tr -d '=')
PAYLOAD=$(echo -n "{\"jti\":\"${API_SID}-${NOW}\",\"iss\":\"${API_SID}\",\"exp\":${EXP},\"rest_api\":true}" | base64 -w 0 | tr '+/' '-_' | tr -d '=')

SIG=$(echo -n "${HEADER}.${PAYLOAD}" | openssl dgst -sha256 -hmac "${API_SECRET}" -binary | base64 -w 0 | tr '+/' '-_' | tr -d '=')

echo "${HEADER}.${PAYLOAD}.${SIG}"
