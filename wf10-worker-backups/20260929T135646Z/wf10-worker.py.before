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
        "audio_base64": audio,
        "followup_id": job.get("followup_id"),
        "uniqueid": job.get("uniqueid"),
        "suppress_telegram": bool(job.get("suppress_telegram", False))
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
