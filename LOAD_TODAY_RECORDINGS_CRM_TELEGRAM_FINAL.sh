#!/usr/bin/env bash
set -euo pipefail

QUEUE="/var/spool/wf10/queue"
DONE="/var/spool/wf10/done"

mkdir -p "$QUEUE" "$DONE"

echo "============================================================"
echo "WF10 LOAD TODAY ANSWERED RECORDINGS"
echo "============================================================"

echo
echo "1. STOP WORKER"

systemctl stop wf10-recording-worker || true


echo
echo "2. CLEAN OLD EMPTY QUEUE"

find "$QUEUE" -name "*.json" -delete


echo
echo "3. BUILD CLEAN QUEUE FROM TODAY ANSWERED"

mariadb asterisk -N -B -e "
SELECT
w.phone,
w.followup_id,
c.uniqueid,
c.billsec
FROM wf_call_followups w
JOIN cdr c
ON RIGHT(CONVERT(w.phone USING utf8mb4),10)
 =
RIGHT(CONVERT(c.dst USING utf8mb4),10)
WHERE
w.provider='asterisk'
AND w.call_status='ANSWERED'
AND COALESCE(w.recording_synced,0)=0
AND c.disposition='ANSWERED'
AND c.calldate >= UTC_DATE()
AND c.billsec > 0;
" | while IFS=$'\t' read -r phone fid uniqueid duration
do

WAV="/tmp/test-${uniqueid}.wav"

if [ ! -f "$WAV" ]; then
    echo "SKIP WAV MISSING $uniqueid"
    continue
fi

SIZE=$(stat -c%s "$WAV")

if [ "$SIZE" -le 44 ]; then
    echo "SKIP EMPTY WAV $uniqueid"
    continue
fi


DATE=$(date -u -d "@${uniqueid%.*}" +"%Y-%m-%d" 2>/dev/null || date -u +"%Y-%m-%d")

NEWNAME="${phone}_${DATE}_${duration}s.wav"

JOB="$QUEUE/${uniqueid}.json"


cat >"$JOB" <<EOF
{
"phone":"$phone",
"followup_id":"$fid",
"uniqueid":"$uniqueid",
"wav":"$WAV",
"filename":"$NEWNAME",
"duration_seconds":$duration,
"suppress_telegram":false,
"backfill":true
}
EOF


done

COUNT=$(find "$QUEUE" -name "*.json" | wc -l)

echo
echo "QUEUE CREATED: $COUNT"


echo
echo "4. START WORKER"

systemctl start wf10-recording-worker


echo
echo "5. STATUS"

systemctl status wf10-recording-worker --no-pager | head -20

echo
echo "DONE"
