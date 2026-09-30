#!/usr/bin/env python3

import os
import re
import csv
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

QUEUE = Path("/var/spool/wf10/queue")
DONE  = Path("/var/spool/wf10/done")

QUEUE.mkdir(parents=True, exist_ok=True)
DONE.mkdir(parents=True, exist_ok=True)

TODAY = "2026-09-25"

# Máxima diferencia permitida entre fin CDR y creación del followup.
MAX_DIFF_SECONDS = 300

def mariadb_tsv(sql):
    p = subprocess.run(
        ["mariadb", "asterisk", "-N", "-B", "-e", sql],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if p.returncode != 0:
        raise RuntimeError(p.stderr)

    return [
        line.split("\t")
        for line in p.stdout.splitlines()
        if line.strip()
    ]

def digits(v):
    return re.sub(r"\D", "", str(v or ""))

def phone10(v):
    d = digits(v)
    return d[-10:] if len(d) >= 10 else d

def dt(v):
    return datetime.strptime(v, "%Y-%m-%d %H:%M:%S")

print("=" * 70)
print("1. FOLLOWUPS ANSWERED SIN GRABACION")
print("=" * 70)

followups = mariadb_tsv(f"""
SELECT
    id,
    phone,
    lead_id,
    followup_id,
    provider,
    call_status,
    COALESCE(recording_synced,0),
    created_at
FROM wf_call_followups
WHERE provider='asterisk'
  AND call_status='ANSWERED'
  AND COALESCE(recording_synced,0)=0
  AND created_at >= '{TODAY} 00:00:00'
  AND created_at <  '{TODAY} 23:59:59'
ORDER BY created_at ASC;
""")

print("Pendientes DB:", len(followups))

print()
print("=" * 70)
print("2. CDR ANSWERED DEL DIA")
print("=" * 70)

cdrs = mariadb_tsv(f"""
SELECT
    calldate,
    dst,
    duration,
    billsec,
    disposition,
    accountcode,
    uniqueid
FROM cdr
WHERE calldate >= '{TODAY} 00:00:00'
  AND calldate <  '{TODAY} 23:59:59'
  AND disposition='ANSWERED'
  AND dcontext='from-client-elevenlabs'
ORDER BY calldate ASC;
""")

print("CDR ANSWERED:", len(cdrs))

# Preparar CDRs
cdr_objects = []

for r in cdrs:
    try:
        calldate, dst, duration, billsec, disposition, accountcode, uniqueid = r

        start = dt(calldate)
        duration = int(float(duration or 0))
        billsec = int(float(billsec or 0))

        end = start + timedelta(seconds=duration)

        cdr_objects.append({
            "phone": dst,
            "phone10": phone10(dst),
            "start": start,
            "end": end,
            "duration": duration,
            "billsec": billsec,
            "uniqueid": uniqueid,
            "accountcode": accountcode,
            "used": False
        })

    except Exception:
        pass

matched = []
unmatched = []
missing_wav = []
empty_wav = []
already_queued = []
queued = []

print()
print("=" * 70)
print("3. CRUZANDO FOLLOWUP -> CDR -> WAV")
print("=" * 70)

for row in followups:

    (
        row_id,
        phone,
        lead_id,
        followup_id,
        provider,
        call_status,
        recording_synced,
        created_at
    ) = row

    ftime = dt(created_at)
    p10 = phone10(phone)

    candidates = []

    for c in cdr_objects:

        if c["used"]:
            continue

        if c["phone10"] != p10:
            continue

        diff = abs((ftime - c["end"]).total_seconds())

        if diff <= MAX_DIFF_SECONDS:
            candidates.append((diff, c))

    if not candidates:
        unmatched.append({
            "phone": phone,
            "followup_id": followup_id,
            "created_at": created_at
        })
        continue

    candidates.sort(key=lambda x: x[0])

    diff, c = candidates[0]
    c["used"] = True

    uniqueid = c["uniqueid"]
    wav = Path(f"/tmp/test-{uniqueid}.wav")

    info = {
        "phone": phone,
        "lead_id": lead_id,
        "followup_id": followup_id,
        "created_at": created_at,
        "uniqueid": uniqueid,
        "billsec": c["billsec"],
        "cdr_end": c["end"].strftime("%Y-%m-%d %H:%M:%S"),
        "difference_seconds": int(diff),
        "wav": str(wav)
    }

    matched.append(info)

    if not wav.exists():
        missing_wav.append(info)
        continue

    size = wav.stat().st_size

    if size <= 44:
        info["size"] = size
        empty_wav.append(info)
        continue

    qfile = QUEUE / f"{uniqueid}.json"
    dfile = DONE / f"{uniqueid}.json"

    if qfile.exists() or dfile.exists():
        already_queued.append(info)
        continue

    job = {
        "phone": phone,
        "wav": str(wav),
        "filename": wav.name,
        "uniqueid": uniqueid,

        # IMPORTANTE:
        # exact followup; el worker NO tiene que adivinar por teléfono.
        "followup_id": followup_id,
        "lead_id": lead_id,

        "queued_at": datetime.now(timezone.utc).timestamp(),
        "attempts": 0,
        "next_attempt": 0,

        "backfill": True,
        "backfill_date": TODAY,
        "cdr_billsec": c["billsec"]
    }

    tmp = Path(str(qfile) + ".tmp")

    with open(tmp, "w") as f:
        json.dump(job, f)

    os.replace(tmp, qfile)

    info["size"] = size
    queued.append(info)

print()
print("=" * 70)
print("4. RESULTADO")
print("=" * 70)

print(f"FOLLOWUPS PENDIENTES : {len(followups)}")
print(f"MATCH CDR            : {len(matched)}")
print(f"ENCOLADAS AHORA      : {len(queued)}")
print(f"YA EN QUEUE/DONE     : {len(already_queued)}")
print(f"SIN WAV              : {len(missing_wav)}")
print(f"WAV VACIO 44 BYTES   : {len(empty_wav)}")
print(f"SIN MATCH CDR        : {len(unmatched)}")

print()
print("=" * 70)
print("5. GRABACIONES ENCOLADAS")
print("=" * 70)

for x in queued:
    print(
        f"{x['phone']} | "
        f"{x['followup_id']} | "
        f"{x['uniqueid']} | "
        f"{x['billsec']}s | "
        f"{x['size']} bytes"
    )

if missing_wav:
    print()
    print("=" * 70)
    print("6. CONTESTADAS PERO WAV NO ENCONTRADO")
    print("=" * 70)

    for x in missing_wav:
        print(
            f"{x['phone']} | "
            f"{x['followup_id']} | "
            f"uniqueid={x['uniqueid']} | "
            f"{x['created_at']}"
        )

if empty_wav:
    print()
    print("=" * 70)
    print("7. WAV VACIOS")
    print("=" * 70)

    for x in empty_wav:
        print(
            f"{x['phone']} | "
            f"uniqueid={x['uniqueid']} | "
            f"{x.get('size',0)} bytes"
        )

if unmatched:
    print()
    print("=" * 70)
    print("8. FOLLOWUPS SIN MATCH CDR")
    print("=" * 70)

    for x in unmatched:
        print(
            f"{x['phone']} | "
            f"{x['followup_id']} | "
            f"{x['created_at']}"
        )

print()
print("=" * 70)
print("QUEUE TOTAL")
print("=" * 70)

print(
    len(list(QUEUE.glob("*.json"))),
    "grabaciones esperando/procesandose"
)

