#!/bin/bash

WEBHOOK_URL="https://landmarket-n8n.dhsoig.easypanel.host/webhook/send-recording"
SENT_LOG="/root/sent_recordings.txt"

mysql -N asterisk -e "SELECT uniqueid, dst, DATE(calldate) FROM cdr WHERE billsec > 60 AND disposition='ANSWERED' AND calldate >= NOW() - INTERVAL 1 DAY;" | while read uid dst calldate; do

  if grep -q "^${uid}$" "$SENT_LOG" 2>/dev/null; then
    continue
  fi

  FILE="/tmp/test-${uid}.wav"

  if [ -f "$FILE" ]; then
    PHONE=$(echo "$dst" | sed 's/^+//')
    base64 -w 0 "$FILE" > /tmp/audio_b64_tmp.txt

    jq -n --arg phone "$PHONE" \
          --arg date "$calldate" \
          --arg filename "${PHONE}_${calldate}.wav" \
          --rawfile audio /tmp/audio_b64_tmp.txt \
          '{phone: $phone, date: $date, filename: $filename, audio_base64: $audio}' \
          > /tmp/payload_tmp.json

    curl -s -X POST "$WEBHOOK_URL" \
      -H "Content-Type: application/json" \
      --data-binary @/tmp/payload_tmp.json \
      -o /dev/null

    echo "$uid" >> "$SENT_LOG"
    echo "Sent: ${PHONE}_${calldate}.wav"
  fi
done

rm -f /tmp/audio_b64_tmp.txt /tmp/payload_tmp.json
