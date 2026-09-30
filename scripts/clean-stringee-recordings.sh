#!/bin/bash
# Borra grabaciones .wav del worker Stringee con más de 2 días
# Seguro: para entonces ya se enviaron a Telegram + LeadStudio (WF10)
RECORDINGS_DIR="/opt/stringee-ai-worker/recordings"
LOG_FILE="/var/log/clean-stringee-recordings.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando limpieza..." >> "$LOG_FILE"

COUNT=$(find "$RECORDINGS_DIR" -iname "*.wav" -mtime +2 | wc -l)
find "$RECORDINGS_DIR" -iname "*.wav" -mtime +2 -delete

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Borrados: $COUNT archivos" >> "$LOG_FILE"
