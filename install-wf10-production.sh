#!/usr/bin/env bash
set -euo pipefail

WFID="TW1CHksqyf66MjQz"
DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/root/wf10-production-backup-$STAMP"

mkdir -p "$BACKUP"
chmod 700 "$BACKUP"

echo
echo "============================================================"
echo "1. BACKUP WF10 + DIALPLAN"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT row_to_json(w)::text FROM workflow_entity w WHERE id='$WFID';" \
> "$BACKUP/workflow-full.json"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT nodes::text FROM workflow_entity WHERE id='$WFID';" \
> "$BACKUP/nodes.json"

cp -a /etc/asterisk/extensions.conf "$BACKUP/extensions.conf"

chmod 600 "$BACKUP"/*

echo "Backup: $BACKUP"

echo
echo "============================================================"
echo "2. EXTRAER CREDENCIALES LEADSTUDIO SIN MOSTRARLAS"
echo "============================================================"

python3 - "$BACKUP/nodes.json" <<'PY'
import json, re, shlex, sys, os

nodes = json.load(open(sys.argv[1], encoding="utf-8"))

email = None
password = None

for n in nodes:
    if "Login LeadStudio" not in n.get("name", ""):
        continue

    raw = n.get("parameters", {}).get("jsonBody", "")

    if isinstance(raw, dict):
        d = raw
    else:
        try:
            d = json.loads(raw)
        except Exception:
            d = {}
            m1 = re.search(r'"email"\s*:\s*"([^"]+)"', str(raw))
            m2 = re.search(r'"password"\s*:\s*"([^"]+)"', str(raw))
            if m1:
                d["email"] = m1.group(1)
            if m2:
                d["password"] = m2.group(1)

    if d.get("email") and d.get("password"):
        email = d["email"]
        password = d["password"]
        break

if not email or not password:
    raise SystemExit("ERROR: no pude extraer login de LeadStudio desde WF10")

with open("/etc/wf10-token-cache.env", "w") as f:
    f.write("LEADSTUDIO_EMAIL=" + shlex.quote(email) + "\n")
    f.write("LEADSTUDIO_PASSWORD=" + shlex.quote(password) + "\n")

os.chmod("/etc/wf10-token-cache.env", 0o600)

print("Credenciales extraidas OK (no mostradas)")
PY

echo
echo "============================================================"
echo "3. TOKEN CACHE LEADSTUDIO"
echo "============================================================"

cat >/usr/local/bin/wf10-token-cache.py <<'PY'
#!/usr/bin/env python3

import os
import json
import time
import base64
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

LOGIN_URL = "https://lead-studio-9gnl.onrender.com/api/auth/login"

EMAIL = os.environ["LEADSTUDIO_EMAIL"]
PASSWORD = os.environ["LEADSTUDIO_PASSWORD"]

token = None
token_exp = 0
lock = threading.Lock()

def jwt_exp(t):
    try:
        p = t.split(".")[1]
        p += "=" * (-len(p) % 4)
        d = json.loads(base64.urlsafe_b64decode(p.encode()))
        return int(d.get("exp", 0))
    except Exception:
        return 0

def login():
    global token, token_exp

    payload = json.dumps({
        "email": EMAIL,
        "password": PASSWORD
    }).encode()

    last_error = None

    for delay in [0, 5, 15, 30, 60]:
        if delay:
            time.sleep(delay)

        try:
            req = urllib.request.Request(
                LOGIN_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())

            t = data.get("accessToken")
            if not t:
                raise RuntimeError("LeadStudio no devolvio accessToken")

            exp = jwt_exp(t)

            token = t

            # Si es JWT, respetar exp.
            # Si no, cache conservador de 2 minutos.
            token_exp = exp if exp else int(time.time()) + 120

            return token

        except urllib.error.HTTPError as e:
            last_error = RuntimeError("HTTP %s en login LeadStudio" % e.code)
            if e.code != 429:
                break
        except Exception as e:
            last_error = e

    raise last_error or RuntimeError("login LeadStudio fallo")

def get_token():
    global token

    now = time.time()

    if token and now < token_exp - 45:
        return token

    with lock:
        now = time.time()

        if token and now < token_exp - 45:
            return token

        return login()

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        return

    def answer(self):
        try:
            t = get_token()
            body = json.dumps({"accessToken": t}).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except Exception as e:
            body = json.dumps({
                "error": "token_unavailable",
                "message": str(e)
            }).encode()

            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        self.answer()

    def do_POST(self):
        # Consumir body que mande n8n, aunque no lo necesitamos.
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            if ln:
                self.rfile.read(ln)
        except Exception:
            pass

        self.answer()

ThreadingHTTPServer(("172.18.0.1", 8092), Handler).serve_forever()
PY

chmod 700 /usr/local/bin/wf10-token-cache.py

cat >/etc/systemd/system/wf10-token-cache.service <<'EOF'
[Unit]
Description=WF10 LeadStudio Token Cache
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/wf10-token-cache.env
ExecStart=/usr/bin/python3 /usr/local/bin/wf10-token-cache.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now wf10-token-cache

sleep 2

systemctl is-active --quiet wf10-token-cache \
  && echo "Token cache: ACTIVO" \
  || { systemctl status wf10-token-cache --no-pager; exit 1; }

echo
echo "============================================================"
echo "4. PATCH SEGURO DE WF10"
echo "============================================================"

python3 - "$BACKUP/nodes.json" "$BACKUP/nodes-patched.json" <<'PY'
import json, sys

src, dst = sys.argv[1], sys.argv[2]
nodes = json.load(open(src, encoding="utf-8"))

LOGIN = {
    "🔐 Login LeadStudio (WF10-Asterisk)2",
    "🔐 Login LeadStudio (WF10-Stringee)2",
}

PUTS = {
    "📤 PUT Recording — LeadStudio (Asterisk)2",
    "📤 PUT Recording — LeadStudio (Stringee)2",
}

TELEGRAM = {
    "Send to Telegram5",
    "Send to Telegram6",
    "Send to Telegram (Stringee)3",
    "Send to Telegram1 (Stringee)3",
}

def strict_http(n):
    p = n.setdefault("parameters", {})
    opt = p.setdefault("options", {})
    response1 = opt.setdefault("response", {})
    response2 = response1.setdefault("response", {})
    response2["neverError"] = False

    n["retryOnFail"] = False
    n["continueOnFail"] = False
    n.pop("onError", None)

for n in nodes:
    name = n.get("name", "")
    p = n.setdefault("parameters", {})

    # Login: todos comparten un token cache.
    if name in LOGIN:
        p["url"] = "http://172.18.0.1:8092/token"
        p["method"] = "POST"
        p["sendBody"] = False
        p.pop("jsonBody", None)

        strict_http(n)

    # PUT: un 4xx/429/5xx DEBE detener la rama.
    # Nunca marcar synced si LeadStudio rechazo el WAV.
    if name in PUTS:
        strict_http(n)

    # Asterisk: no ocultar errores de item mapping.
    if name == "🔗 Attach Audio + Token (Asterisk)2":
        p["mode"] = "runOnceForEachItem"
        p["jsCode"] = r"""const token = $input.first().json.accessToken;
const lookup = $('🔍 Lookup Followup ID (Asterisk)2').first().json;
const original = $('Convert Base64 to Binary2').first();

if (!token) {
  throw new Error('WF10 Asterisk: accessToken ausente');
}

if (!lookup.followup_id) {
  throw new Error('WF10 Asterisk: followup_id ausente');
}

if (!original.binary || !original.binary.data) {
  throw new Error('WF10 Asterisk: audio binary ausente');
}

return {
  json: {
    ...original.json,
    accessToken: token,
    followup_id: lookup.followup_id,
    lead_id: lookup.lead_id
  },
  binary: original.binary
};"""
        n["retryOnFail"] = False
        n["continueOnFail"] = False
        n.pop("onError", None)

    # Stringee: mantener pairing por item pero NO esconder errores.
    if name == "🔗 Attach Audio + Token (Stringee)2":
        p["mode"] = "runOnceForEachItem"
        p["jsCode"] = r"""const token = $input.item.json.accessToken;
const lookup = $('🔍 Lookup Followup ID (Stringee)2').item.json;
const original = $('🔄 Fetch & Convert Stringee Recordings2').item;

if (!token) {
  throw new Error('WF10 Stringee: accessToken ausente');
}

if (!lookup.followup_id) {
  throw new Error('WF10 Stringee: followup_id ausente');
}

if (!original.binary || !original.binary.data) {
  throw new Error('WF10 Stringee: audio binary ausente');
}

return {
  json: {
    ...original.json,
    accessToken: token,
    followup_id: lookup.followup_id,
    lead_id: lookup.lead_id
  },
  binary: original.binary
};"""
        n["retryOnFail"] = False
        n["continueOnFail"] = False
        n.pop("onError", None)

    # Asterisk: asegurar que el flag synced usa SIEMPRE el followup correcto.
    if name == "💾 Mark Recording Synced (Asterisk)2":
        p["query"] = """UPDATE wf_call_followups
SET recording_synced = 1
WHERE followup_id = '{{ $('🔗 Attach Audio + Token (Asterisk)2').first().json.followup_id }}';"""
        n["retryOnFail"] = True
        n["maxTries"] = 3
        n["continueOnFail"] = False
        n.pop("onError", None)

    # Stringee: idem, manteniendo item pairing.
    if name == "💾 Mark Recording Synced (Stringee)3":
        p["query"] = """UPDATE wf_call_followups
SET recording_synced = 1
WHERE followup_id = '{{ $('🔗 Attach Audio + Token (Stringee)2').item.json.followup_id }}';"""
        n["retryOnFail"] = True
        n["maxTries"] = 3
        n["continueOnFail"] = False
        n.pop("onError", None)

    # Telegram puede reintentar si Telegram responde 429/transitorio.
    if name in TELEGRAM:
        n["retryOnFail"] = True
        n["maxTries"] = 5
        n["waitBetweenTries"] = 3000

json.dump(nodes, open(dst, "w", encoding="utf-8"),
          ensure_ascii=False, separators=(",", ":"))

print("WF10 JSON parcheado OK")
PY

{
  echo "UPDATE workflow_entity SET nodes = \$WF10JSON\$"
  cat "$BACKUP/nodes-patched.json"
  echo "\$WF10JSON\$::json WHERE id='$WFID';"
} | docker exec -i "$DB" psql -U postgres -d landmarket >/dev/null

echo "Workflow actualizado en PostgreSQL"

echo
echo "============================================================"
echo "5. COLA PERSISTENTE PARA ASTERISK"
echo "============================================================"

install -d -o asterisk -g asterisk -m 0770 /var/spool/wf10
install -d -o asterisk -g asterisk -m 0770 /var/spool/wf10/queue
install -d -o root -g root -m 0750 /var/spool/wf10/done

touch /var/log/asterisk/wf10-enqueue.log
touch /var/log/asterisk/wf10-worker.log

chown asterisk:asterisk /var/log/asterisk/wf10-enqueue.log
chown root:root /var/log/asterisk/wf10-worker.log

# Retirar uploader de prueba anterior si existe.
if [ -f /usr/local/bin/send-recording-wf10.py ]; then
    mv /usr/local/bin/send-recording-wf10.py \
       "/usr/local/bin/send-recording-wf10.py.old-$STAMP"
fi

cat >/usr/local/bin/wf10-enqueue.py <<'PY'
#!/usr/bin/env python3

import sys
import os
import json
import time
from datetime import datetime, timezone

QUEUE = "/var/spool/wf10/queue"
DONE = "/var/spool/wf10/done"
LOG = "/var/log/asterisk/wf10-enqueue.log"

def log(s):
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(LOG, "a") as f:
            f.write(f"{ts} | {s}\n")
    except Exception:
        pass

if len(sys.argv) < 5:
    sys.exit(1)

phone = sys.argv[1]
wav = sys.argv[2]
uniqueid = sys.argv[3]
dialstatus = sys.argv[4].upper().strip()

# Solo llamadas realmente ANSWERED.
if dialstatus != "ANSWER":
    sys.exit(0)

# StopMixMonitor ya se ejecuto, pero permitimos un instante para flush final.
for _ in range(10):
    if os.path.isfile(wav) and os.path.getsize(wav) > 44:
        break
    time.sleep(0.2)

if not os.path.isfile(wav):
    log(f"{uniqueid} | {phone} | ERROR WAV NO EXISTE | {wav}")
    sys.exit(1)

size = os.path.getsize(wav)

if size <= 44:
    log(f"{uniqueid} | {phone} | SKIP WAV VACIO | {size} bytes")
    sys.exit(0)

job = {
    "phone": phone,
    "wav": wav,
    "filename": os.path.basename(wav),
    "uniqueid": uniqueid,
    "queued_at": time.time(),
    "attempts": 0,
    "next_attempt": 0,
    "followup_id": None
}

path = os.path.join(QUEUE, uniqueid + ".json")

# Idempotencia: no duplicar el mismo UNIQUEID.
if os.path.exists(path) or os.path.exists(os.path.join(DONE, uniqueid + ".json")):
    sys.exit(0)

tmp = path + ".tmp"

with open(tmp, "w") as f:
    json.dump(job, f)

os.replace(tmp, path)

log(f"{uniqueid} | {phone} | ENCOLADO | {size} bytes")
PY

chmod 755 /usr/local/bin/wf10-enqueue.py

cat >/usr/local/bin/wf10-worker.py <<'PY'
#!/usr/bin/env python3

import os
import re
import json
import time
import base64
import shutil
import subprocess
import threading
import urllib.request
import urllib.error
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

QUEUE = "/var/spool/wf10/queue"
DONE = "/var/spool/wf10/done"
LOG = "/var/log/asterisk/wf10-worker.log"

WEBHOOK = "https://landmarket-n8n.dhsoig.easypanel.host/webhook/send-recording"

MAX_WORKERS = 10

# Límite interno conservador.
# Permite ráfagas de 50 sin problema, pero evita que un backfill enorme
# martillee LeadStudio indefinidamente.
RATE_MAX = 150
RATE_WINDOW = 15 * 60

pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)

inflight = set()
inflight_lock = threading.Lock()

rate_times = deque()
rate_lock = threading.Lock()

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LOG, "a") as f:
        f.write(f"{ts} | {msg}\n")
        f.flush()

def sql_scalar(sql):
    p = subprocess.run(
        ["mariadb", "asterisk", "-N", "-B", "-e", sql],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15
    )

    if p.returncode != 0:
        raise RuntimeError("MariaDB: " + p.stderr.strip())

    rows = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    return rows[0] if rows else ""

def esc_sql(v):
    return str(v).replace("\\", "\\\\").replace("'", "''")

def get_followup(job):
    # Backfill futuro puede proporcionar ID exacto.
    if job.get("followup_id"):
        return job["followup_id"]

    digits = re.sub(r"\D", "", str(job["phone"]))
    last10 = digits[-10:]

    q = f"""
SELECT followup_id
FROM wf_call_followups
WHERE RIGHT(phone,10)='{last10}'
  AND provider='asterisk'
  AND call_status='ANSWERED'
  AND (recording_synced IS NULL OR recording_synced=0)
  AND created_at >= UTC_TIMESTAMP() - INTERVAL 12 HOUR
ORDER BY created_at DESC
LIMIT 1;
"""

    return sql_scalar(q)

def is_synced(fid):
    fid = esc_sql(fid)

    v = sql_scalar(f"""
SELECT COALESCE(recording_synced,0)
FROM wf_call_followups
WHERE followup_id='{fid}'
LIMIT 1;
""")

    return v == "1"

def save_job(path, job):
    tmp = path + ".tmp." + str(os.getpid())

    with open(tmp, "w") as f:
        json.dump(job, f)

    os.replace(tmp, path)

def reschedule(path, job, reason):
    job["attempts"] = int(job.get("attempts", 0)) + 1

    delays = [10, 20, 30, 60, 120, 300, 600, 900]
    delay = delays[min(job["attempts"] - 1, len(delays) - 1)]

    job["next_attempt"] = time.time() + delay
    job["last_error"] = reason[:1000]

    save_job(path, job)

    log(
        f"{job['uniqueid']} | {job['phone']} | RETRY "
        f"{job['attempts']} en {delay}s | {reason}"
    )

def finish(path, job):
    job["completed_at"] = time.time()
    job["next_attempt"] = 0

    save_job(path, job)

    dest = os.path.join(DONE, os.path.basename(path))

    if os.path.exists(dest):
        os.remove(dest)

    shutil.move(path, dest)

    log(
        f"{job['uniqueid']} | {job['phone']} | OK "
        f"followup={job.get('followup_id')}"
    )

def rate_acquire():
    while True:
        now = time.time()

        with rate_lock:
            while rate_times and now - rate_times[0] >= RATE_WINDOW:
                rate_times.popleft()

            if len(rate_times) < RATE_MAX:
                rate_times.append(now)
                return

            wait_for = RATE_WINDOW - (now - rate_times[0]) + 0.25

        time.sleep(min(max(wait_for, 0.25), 5))

def post_webhook(job):
    wav = job["wav"]

    if not os.path.isfile(wav):
        raise RuntimeError("WAV desaparecio: " + wav)

    size = os.path.getsize(wav)

    if size <= 44:
        raise RuntimeError(f"WAV vacio: {size} bytes")

    with open(wav, "rb") as f:
        audio = base64.b64encode(f.read()).decode("ascii")

    dt = datetime.fromtimestamp(
        os.path.getmtime(wav),
        tz=timezone.utc
    ).strftime("%Y-%m-%d")

    payload = {
        "phone": job["phone"],
        "date": dt,
        "filename": os.path.basename(wav),
        "audio_base64": audio
    }

    data = json.dumps(payload).encode()

    rate_acquire()

    req = urllib.request.Request(
        WEBHOOK,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=45) as r:
        body = r.read().decode(errors="replace")

        if r.status < 200 or r.status >= 300:
            raise RuntimeError(
                f"Webhook HTTP {r.status}: {body[:300]}"
            )

def process(path):
    try:
        with open(path) as f:
            job = json.load(f)

        # Si otro intento ya termino, no volver a subir.
        if job.get("followup_id") and is_synced(job["followup_id"]):
            finish(path, job)
            return

        fid = get_followup(job)

        if not fid:
            reschedule(
                path,
                job,
                "followup ANSWERED aun no disponible"
            )
            return

        job["followup_id"] = fid
        save_job(path, job)

        if is_synced(fid):
            finish(path, job)
            return

        post_webhook(job)

        # El webhook responde al arrancar el WF.
        # Esperar que LeadStudio PUT + UPDATE terminen.
        for _ in range(30):
            time.sleep(2)

            if is_synced(fid):
                finish(path, job)
                return

        reschedule(
            path,
            job,
            "WF10 ejecuto pero recording_synced sigue en 0"
        )

    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode(errors="replace")[:300]
        except Exception:
            body = ""

        try:
            with open(path) as f:
                job = json.load(f)

            reschedule(
                path,
                job,
                f"HTTP {e.code} webhook: {body}"
            )
        except Exception as ee:
            log(f"{path} | ERROR doble: {e} / {ee}")

    except Exception as e:
        try:
            with open(path) as f:
                job = json.load(f)

            reschedule(path, job, str(e))

        except Exception as ee:
            log(f"{path} | ERROR doble: {e} / {ee}")

    finally:
        with inflight_lock:
            inflight.discard(path)

def main():
    log(
        f"WORKER START | workers={MAX_WORKERS} "
        f"rate={RATE_MAX}/{RATE_WINDOW}s"
    )

    while True:
        try:
            files = sorted(
                os.path.join(QUEUE, x)
                for x in os.listdir(QUEUE)
                if x.endswith(".json")
            )

            now = time.time()

            for path in files:
                try:
                    with open(path) as f:
                        job = json.load(f)

                    if float(job.get("next_attempt", 0)) > now:
                        continue

                except Exception as e:
                    log(f"{path} | JSON ERROR | {e}")
                    continue

                with inflight_lock:
                    if path in inflight:
                        continue

                    inflight.add(path)

                pool.submit(process, path)

            time.sleep(0.5)

        except Exception as e:
            log("MAIN ERROR | " + str(e))
            time.sleep(2)

if __name__ == "__main__":
    main()
PY

chmod 700 /usr/local/bin/wf10-worker.py

cat >/etc/systemd/system/wf10-recording-worker.service <<'EOF'
[Unit]
Description=WF10 Persistent Recording Queue
After=network-online.target mariadb.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/wf10-worker.py
Restart=always
RestartSec=2
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now wf10-recording-worker

echo
echo "============================================================"
echo "6. PATCH DIALPLAN ASTERISK"
echo "============================================================"

python3 <<'PY'
from pathlib import Path

p = Path("/etc/asterisk/extensions.conf")
s = p.read_text()

if "wf10-enqueue.py" in s:
    print("Dialplan ya tenia WF10 enqueue; no se duplica.")
    raise SystemExit(0)

mex_old = """ same => n,MixMonitor(/tmp/test-${UNIQUEID}.wav,b)
 same => n,Dial(PJSIP/${EXTEN}@proveedor-mx,60)
 same => n,Hangup()"""

mex_new = """ same => n,Set(RECORDING=/tmp/test-${UNIQUEID}.wav)
 same => n,MixMonitor(${RECORDING},b)
 same => n,Dial(PJSIP/${EXTEN}@proveedor-mx,60)
 same => n,StopMixMonitor()
 same => n,System(/usr/local/bin/wf10-enqueue.py "${EXTEN}" "${RECORDING}" "${UNIQUEID}" "${DIALSTATUS}")
 same => n,Hangup()"""

nepal_old = """ same => n,MixMonitor(/tmp/test-${UNIQUEID}.wav,b)
 same => n,Set(NEPAL_NUMBER=${EXTEN:1})
 same => n,Dial(PJSIP/${NEPAL_NUMBER}@proveedor1,60)
 same => n,Hangup()"""

nepal_new = """ same => n,Set(RECORDING=/tmp/test-${UNIQUEID}.wav)
 same => n,MixMonitor(${RECORDING},b)
 same => n,Set(NEPAL_NUMBER=${EXTEN:1})
 same => n,Dial(PJSIP/${NEPAL_NUMBER}@proveedor1,60)
 same => n,StopMixMonitor()
 same => n,System(/usr/local/bin/wf10-enqueue.py "${EXTEN}" "${RECORDING}" "${UNIQUEID}" "${DIALSTATUS}")
 same => n,Hangup()"""

india_old = """ same => n,MixMonitor(/tmp/test-${UNIQUEID}.wav,b)
 same => n,Dial(PJSIP/${EXTEN}@proveedor1,60)
 same => n,Hangup()"""

india_new = """ same => n,Set(RECORDING=/tmp/test-${UNIQUEID}.wav)
 same => n,MixMonitor(${RECORDING},b)
 same => n,Dial(PJSIP/${EXTEN}@proveedor1,60)
 same => n,StopMixMonitor()
 same => n,System(/usr/local/bin/wf10-enqueue.py "${EXTEN}" "${RECORDING}" "${UNIQUEID}" "${DIALSTATUS}")
 same => n,Hangup()"""

checks = [
    ("Mexico", mex_old, mex_new),
    ("Nepal", nepal_old, nepal_new),
    ("India", india_old, india_new),
]

for name, old, new in checks:
    if old not in s:
        raise SystemExit(
            f"ERROR: bloque {name} no coincide. NO se escribio nada."
        )

    s = s.replace(old, new, 1)

p.write_text(s)

print("Dialplan parcheado: Mexico + Nepal + India")
PY

asterisk -rx "dialplan reload" >/dev/null

echo "Dialplan recargado"

echo
echo "============================================================"
echo "7. REINICIAR N8N PARA CARGAR WF10 PARCHEADO"
echo "============================================================"

docker service update --force landmarket_n8n >/dev/null

for i in $(seq 1 60); do
    R=$(docker service ls \
        --filter name=landmarket_n8n \
        --format '{{.Name}} {{.Replicas}}' \
        | awk '$1=="landmarket_n8n"{print $2}')

    if [ "$R" = "1/1" ]; then
        break
    fi

    sleep 1
done

sleep 5

echo "n8n replicas:"
docker service ls --filter name=landmarket_n8n \
    --format '{{.Name}} {{.Replicas}}'

echo
echo "============================================================"
echo "8. VALIDACIONES"
echo "============================================================"

echo "-- Token broker --"
N8N=$(docker ps -q --filter name=landmarket_n8n | head -1)

docker exec "$N8N" node -e "
fetch('http://172.18.0.1:8092/token',{method:'POST'})
.then(r=>r.json())
.then(j=>{
  if(!j.accessToken) throw new Error(JSON.stringify(j));
  console.log('TOKEN_CACHE_OK');
})
.catch(e=>{
  console.error('TOKEN_CACHE_PENDING:',e.message);
  process.exitCode=0;
});
"

echo
echo "-- Webhook activo --"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT
    w.id,
    w.name,
    w.active,
    wh.\"webhookPath\"
FROM webhook_entity wh
JOIN workflow_entity w
  ON w.id=wh.\"workflowId\"
WHERE wh.\"webhookPath\"='send-recording';
"

echo
echo "-- Login nodes ahora usan cache --"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT
    n->>'name' AS node,
    n->'parameters'->>'url' AS url
FROM workflow_entity w,
LATERAL jsonb_array_elements(w.nodes::jsonb) n
WHERE w.id='$WFID'
  AND n->>'name' LIKE '%Login LeadStudio%';
"

echo
echo "-- Worker --"

systemctl status wf10-recording-worker --no-pager | head -8

echo
echo "-- Token service --"

systemctl status wf10-token-cache --no-pager | head -8

echo
echo "-- Dialplan --"

asterisk -rx "dialplan show from-client-elevenlabs" \
 | grep -E 'Salida|RECORDING|MixMonitor|Dial|StopMixMonitor|wf10-enqueue'

echo
echo "============================================================"
echo "9. RECONCILIAR LLAMADA DE PRUEBA YA SUBIDA"
echo "============================================================"

mariadb asterisk -e "
UPDATE wf_call_followups
SET recording_synced=1
WHERE followup_id='9a4bf37b-0f70-45e3-b3f4-cf2ae1f629a2'
  AND phone='+919916921120'
  AND call_status='ANSWERED';
"

mariadb asterisk -e "
SELECT
 phone,
 followup_id,
 provider,
 call_status,
 recording_synced,
 created_at
FROM wf_call_followups
WHERE RIGHT(phone,10)='9916921120'
ORDER BY created_at DESC;
"

echo
echo "============================================================"
echo "INSTALACION TERMINADA"
echo "============================================================"

echo "Queue:"
find /var/spool/wf10/queue -maxdepth 1 -name '*.json' | wc -l

echo "Done:"
find /var/spool/wf10/done -maxdepth 1 -name '*.json' | wc -l

echo
echo "Logs:"
echo "  tail -f /var/log/asterisk/wf10-enqueue.log"
echo "  tail -f /var/log/asterisk/wf10-worker.log"
echo "  journalctl -u wf10-token-cache -f"

