#!/usr/bin/env bash
set -euo pipefail

WFID="TW1CHksqyf66MjQz"
DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)
STAMP=$(date +%Y%m%d-%H%M%S)

echo "============================================================"
echo "1. BACKUP"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT nodes::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-nodes-before-ratefix-$STAMP.json"

echo "Backup OK"

echo
echo "============================================================"
echo "2. PATCH TODOS LOS LOGIN NODES"
echo "============================================================"

python3 - "/root/wf10-nodes-before-ratefix-$STAMP.json" /tmp/wf10-ratefix.json <<'PY'
import json, sys

src, dst = sys.argv[1], sys.argv[2]
nodes = json.load(open(src, encoding="utf-8"))

changed = []

for n in nodes:
    name = n.get("name", "")

    if "Login LeadStudio" not in name:
        continue

    p = n.setdefault("parameters", {})

    p["method"] = "POST"
    p["url"] = "http://172.18.0.1:8092/token"
    p["sendBody"] = False

    p.pop("jsonBody", None)
    p.pop("bodyParameters", None)

    opt = p.setdefault("options", {})
    response = opt.setdefault("response", {})
    response2 = response.setdefault("response", {})
    response2["neverError"] = False
    response2["responseFormat"] = "json"

    n["retryOnFail"] = False
    n["continueOnFail"] = False
    n.pop("onError", None)

    changed.append(name)

json.dump(
    nodes,
    open(dst, "w", encoding="utf-8"),
    ensure_ascii=False,
    separators=(",", ":")
)

print("Parcheados:")
for x in changed:
    print(" -", x)
PY

{
  echo "UPDATE workflow_entity SET nodes = \$WF10JSON\$"
  cat /tmp/wf10-ratefix.json
  echo "\$WF10JSON\$::json WHERE id='$WFID';"
} | docker exec -i "$DB" psql -U postgres -d landmarket >/dev/null

echo
echo "============================================================"
echo "3. TOKEN CACHE CON COOLDOWN 429"
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

next_login_allowed = 0
consecutive_429 = 0
last_error = None

lock = threading.Lock()

def jwt_exp(t):
    try:
        p = t.split(".")[1]
        p += "=" * (-len(p) % 4)
        d = json.loads(base64.urlsafe_b64decode(p.encode()))
        return int(d.get("exp", 0))
    except Exception:
        return 0

def perform_login():
    global token
    global token_exp
    global next_login_allowed
    global consecutive_429
    global last_error

    now = time.time()

    if now < next_login_allowed:
        wait = int(next_login_allowed - now)
        raise RuntimeError(
            f"LeadStudio login en cooldown; retry en ~{wait}s"
        )

    payload = json.dumps({
        "email": EMAIL,
        "password": PASSWORD
    }).encode()

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
        token_exp = exp if exp else int(time.time()) + 120

        consecutive_429 = 0
        next_login_allowed = 0
        last_error = None

        return token

    except urllib.error.HTTPError as e:

        if e.code == 429:
            consecutive_429 += 1

            # No seguir golpeando el endpoint.
            # 1º 429: 2 minutos
            # 2º:    5 minutos
            # 3º+:  15 minutos
            if consecutive_429 == 1:
                cooldown = 120
            elif consecutive_429 == 2:
                cooldown = 300
            else:
                cooldown = 900

            next_login_allowed = time.time() + cooldown
            last_error = f"HTTP 429; cooldown {cooldown}s"

            raise RuntimeError(last_error)

        last_error = f"HTTP {e.code} login LeadStudio"
        next_login_allowed = time.time() + 60

        raise RuntimeError(last_error)

    except Exception as e:
        last_error = str(e)
        next_login_allowed = time.time() + 60
        raise

def get_token():
    global token

    now = time.time()

    if token and now < token_exp - 45:
        return token

    with lock:
        now = time.time()

        if token and now < token_exp - 45:
            return token

        return perform_login()

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, obj):
        body = json.dumps(obj).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        self.wfile.write(body)

    def do_GET(self):
        self.handle_token()

    def do_POST(self):
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            if ln:
                self.rfile.read(ln)
        except Exception:
            pass

        self.handle_token()

    def handle_token(self):
        try:
            t = get_token()

            self.send_json(200, {
                "accessToken": t
            })

        except Exception as e:
            self.send_json(503, {
                "error": "token_unavailable",
                "message": str(e)
            })

ThreadingHTTPServer(
    ("172.18.0.1", 8092),
    Handler
).serve_forever()
PY

chmod 700 /usr/local/bin/wf10-token-cache.py

systemctl restart wf10-token-cache

echo
echo "============================================================"
echo "4. RESTART N8N"
echo "============================================================"

docker service update --force landmarket_n8n >/dev/null

for i in $(seq 1 60); do
    R=$(docker service ls \
      --filter name=landmarket_n8n \
      --format '{{.Name}} {{.Replicas}}' \
      | awk '$1=="landmarket_n8n"{print $2}')

    [ "$R" = "1/1" ] && break

    sleep 1
done

sleep 5

echo
echo "============================================================"
echo "5. VERIFICAR LOGIN NODES"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT
 n->>'name' AS node,
 COALESCE(n->>'disabled','false') AS disabled,
 n->'parameters'->>'url' AS url
FROM workflow_entity w,
LATERAL jsonb_array_elements(w.nodes::jsonb) n
WHERE w.id='$WFID'
AND n->>'name' LIKE '%Login LeadStudio%'
ORDER BY 1;
"

echo
echo "============================================================"
echo "6. ESTADO SERVICIOS"
echo "============================================================"

systemctl is-active wf10-token-cache
systemctl is-active wf10-recording-worker

echo
echo "Queue pendiente:"
find /var/spool/wf10/queue -maxdepth 1 -name '*.json' | wc -l

echo
echo "Done:"
find /var/spool/wf10/done -maxdepth 1 -name '*.json' | wc -l

echo
echo "============================================================"
echo "FIX TERMINADO"
echo "============================================================"
