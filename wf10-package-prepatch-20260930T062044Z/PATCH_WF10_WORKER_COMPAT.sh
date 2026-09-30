#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:-check}"
WORKER=/usr/local/bin/wf10-worker.py
BACKUP_ROOT=/root/wf10-worker-backups
MARKER='WF10_WORKER_COMPAT_20260929'

need_root(){ [ "$(id -u)" = 0 ] || { echo 'ERROR: ejecutar como root' >&2; exit 1; }; }
need_worker(){ [ -f "$WORKER" ] || { echo "ERROR: no existe $WORKER" >&2; exit 1; }; }
latest_backup(){ ls -1d "$BACKUP_ROOT"/* 2>/dev/null | sort -r | head -1 || true; }

case "$ACTION" in
  check)
    need_worker
    echo '== WF10 WORKER COMPAT CHECK =='
    grep -nE 'def get_followup\(|urlopen\(req, timeout=|post_webhook\(job\)|WF10_WORKER_COMPAT_20260929' "$WORKER" || true
    if grep -q "$MARKER" "$WORKER"; then
      echo 'OK: compatibilidad ya aplicada.'
    else
      echo 'PENDIENTE: se ajustará match por UNIQUEID + timeout 120s + respuesta final del webhook.'
    fi
    ;;

  apply)
    need_root; need_worker
    if grep -q "$MARKER" "$WORKER"; then
      echo 'OK: worker ya parcheado; no se cambia nada.'
      python3 -m py_compile "$WORKER"
      exit 0
    fi
    STAMP=$(date -u +%Y%m%dT%H%M%SZ)
    B="$BACKUP_ROOT/$STAMP"
    mkdir -p "$B"
    cp -a "$WORKER" "$B/wf10-worker.py.before"
    systemctl is-active wf10-recording-worker > "$B/service_was_active" 2>/dev/null || true

    python3 - "$WORKER" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1]); s=p.read_text()
marker='WF10_WORKER_COMPAT_20260929'

m=re.search(r'def get_followup\(job\):\n.*?\n(?=def is_synced\(fid\):)', s, re.S)
if not m:
    raise SystemExit('No encuentro bloque get_followup esperado; NO se modificó.')
new_get=r'''def get_followup(job):
    # WF10_WORKER_COMPAT_20260929
    # Para jobs nuevos, usar el mismo dueño temporal que WF10 usando UNIQUEID.
    # Evita que llamadas simultáneas al mismo teléfono hagan que el worker espere
    # un followup distinto al que WF10 realmente marcó.
    if job.get("followup_id"):
        return job["followup_id"]

    digits = re.sub(r"\D", "", str(job.get("phone") or ""))
    last10 = digits[-10:]
    uid = str(job.get("uniqueid") or "")
    mm = re.search(r"(\d{9,11})\.\d+", uid)

    if mm and len(last10) == 10:
        start = int(mm.group(1))
        q = f"""
SELECT followup_id
FROM wf_call_followups
WHERE provider='asterisk'
  AND followup_id <> 'undefined'
  AND phone <> 'undefined'
  AND RIGHT(phone,10)='{last10}'
  AND created_at >= FROM_UNIXTIME({start} - 120)
  AND created_at <= FROM_UNIXTIME({start} + 1800)
ORDER BY created_at ASC, id ASC
LIMIT 1;
"""
        return sql_scalar(q)

    # Fallback sólo para jobs heredados que no tengan UNIQUEID.
    q = f"""
SELECT followup_id
FROM wf_call_followups
WHERE RIGHT(phone,10)='{last10}'
  AND provider='asterisk'
  AND (recording_synced IS NULL OR recording_synced=0)
  AND created_at >= UTC_TIMESTAMP() - INTERVAL 12 HOUR
ORDER BY created_at DESC
LIMIT 1;
"""
    return sql_scalar(q)

'''
s2=s[:m.start()] + new_get + s[m.end():]

old=re.compile(r'''    with urllib\.request\.urlopen\(req, timeout=45\) as r:\n        body = r\.read\(\)\.decode\(errors="replace"\)\n\n        if r\.status < 200 or r\.status >= 300:\n            raise RuntimeError\(\n                f"Webhook HTTP \{r\.status\}: \{body\[:300\]\}"\n            \)''')
rep='''    with urllib.request.urlopen(req, timeout=120) as r:\n        body = r.read().decode(errors="replace")\n\n        if r.status < 200 or r.status >= 300:\n            raise RuntimeError(\n                f"Webhook HTTP {r.status}: {body[:300]}"\n            )\n\n        try:\n            return json.loads(body) if body else {}\n        except Exception:\n            return {"raw": body[:300]}'''
s3,n=old.subn(rep,s2,count=1)
if n!=1:
    raise SystemExit('No encuentro bloque urlopen timeout=45 esperado; NO se modificó.')

needle='''        post_webhook(job)\n\n        # El webhook responde al arrancar el WF.\n        # Esperar que LeadStudio PUT + UPDATE terminen.\n'''
if needle not in s3:
    raise SystemExit('No encuentro llamada post_webhook esperada; NO se modificó.')
replacement='''        resp = post_webhook(job)\n\n        # WF10 nuevo responde al FINAL con el estado real.\n        status = str((resp or {}).get("status") or "")\n        returned_fid = (resp or {}).get("followup_id")\n        if returned_fid:\n            fid = str(returned_fid)\n            job["followup_id"] = fid\n            save_job(path, job)\n\n        # Estados terminales: no volver a mandar esta grabación.\n        if status in ("attached", "attached_already_synced", "not_pending", "invalid_audio", "put_rejected"):\n            finish(path, job)\n            return\n\n        # Compatibilidad defensiva: si alguna respuesta 200 futura pide retry.\n        if status in ("put_failed", "followup_not_found"):\n            reschedule(path, job, "WF10: " + status)\n            return\n\n        # Respuesta desconocida/legacy: comprobar DB antes de reintentar.\n'''
s4=s3.replace(needle,replacement,1)
p.write_text(s4)
PY

    python3 -m py_compile "$WORKER" || { cp -a "$B/wf10-worker.py.before" "$WORKER"; echo 'ERROR: py_compile falló; restaurado.'; exit 1; }
    echo "$B" > /root/.wf10-worker-last-backup
    echo "OK: worker parcheado. Backup: $B"
    grep -nE 'WF10_WORKER_COMPAT_20260929|urlopen\(req, timeout=120\)|status in \(' "$WORKER" | head -20
    ;;

  rollback)
    need_root
    B="${2:-$(cat /root/.wf10-worker-last-backup 2>/dev/null || true)}"
    [ -n "$B" ] && [ -f "$B/wf10-worker.py.before" ] || { echo 'ERROR: no encuentro backup worker.' >&2; exit 1; }
    cp -a "$B/wf10-worker.py.before" "$WORKER"
    python3 -m py_compile "$WORKER"
    systemctl restart wf10-recording-worker || true
    echo "OK: worker restaurado desde $B"
    ;;

  status)
    need_worker
    systemctl is-active wf10-recording-worker 2>/dev/null || true
    grep -nE 'WF10_WORKER_COMPAT_20260929|urlopen\(req, timeout=' "$WORKER" || true
    ;;

  *) echo "uso: $0 check|apply|status|rollback [backup_dir]" >&2; exit 2;;
esac
