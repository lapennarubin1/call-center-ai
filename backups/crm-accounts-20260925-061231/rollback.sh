#!/bin/bash
# Rollback del fix 24/09 — deja el panel exactamente como estaba.
cp -p "/root/backups/crm-accounts-20260925-061231/analytics.py" "/root/backups/crm-accounts-20260925-061231/server.py" "/opt/landmark-panel/app/"
systemctl disable --now landmark-crm-accounts.timer 2>/dev/null
systemctl restart landmark-panel && echo "panel restaurado"
W=$(ls -t /root/backups/crm-accounts-20260925-061231/WF14-backup-*.json 2>/dev/null | head -1)
[ -n "$W" ] && LM_N8N_API_KEY="${LM_N8N_API_KEY:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI3MmRhYTU4Ny1kYWY1LTQ2OTktYjVkNy04MzllZTYxNmQ4YjciLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiNTBjNTE0YzEtMjAzNC00MWI4LWFhZjctYzFiNjhkYjdhZGEyIiwiaWF0IjoxNzg3MzAxMDMxfQ.Hd18VkvLF2GnVqFVQ_CCOPbpQlI58z7TfeUE0jpaqD4}" BACKUP_DIR="/root/backups/crm-accounts-20260925-061231" python3 "/opt/landmark-panel/tools/n8n_wf14.py" restore "$W"
echo "Las columnas nuevas de crm_conversions quedan (no molestan al código anterior)."
