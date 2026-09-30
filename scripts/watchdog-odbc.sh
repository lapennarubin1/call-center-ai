#!/bin/bash
# watchdog-odbc.sh — corre cada 5 min por cron.
# Si Asterisk pierde la conexión ODBC a MySQL (ej: MariaDB se reinició),
# los CDR dejan de escribirse SIN que Asterisk avise con ningún error
# visible — las llamadas siguen funcionando normal, solo se pierden las
# estadísticas. Esto detecta "0 conexiones activas" y las repara solo.

LOG="/var/log/asterisk-odbc-watchdog.log"

STATUS=$(asterisk -rx "odbc show all" 2>/dev/null)
ACTIVE=$(echo "$STATUS" | grep -oP "Number of active connections: \K[0-9]+")

if [ -z "$ACTIVE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') — no se pudo leer el estado ODBC (¿Asterisk caído?)" >> "$LOG"
    exit 1
fi

if [ "$ACTIVE" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') — ODBC desconectado (0 activas), reconectando..." >> "$LOG"
    asterisk -rx "module reload res_odbc.so" && sleep 1 && asterisk -rx "module reload cdr_adaptive_odbc.so" >> "$LOG" 2>&1
    sleep 2
    NEW_STATUS=$(asterisk -rx "odbc show all" 2>/dev/null)
    NEW_ACTIVE=$(echo "$NEW_STATUS" | grep -oP "Number of active connections: \K[0-9]+")
    if [ "$NEW_ACTIVE" -gt 0 ] 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') — reconectado OK ($NEW_ACTIVE activas)" >> "$LOG"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') — SIGUE CAÍDO tras reload, revisar manualmente" >> "$LOG"
        # Opcional: mandar alerta (Telegram, etc.) acá si querés que te avise
    fi
fi
