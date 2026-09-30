#!/usr/bin/env python3

import base64
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BASE="https://lead-studio-9gnl.onrender.com"
LIST_URL="http://172.18.0.1:8091/recordings"
TOKEN_URL="http://172.18.0.1:8092/token"

TODAY=datetime.now(timezone.utc).strftime("%Y-%m-%d")

def sh(cmd):
    p=subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return p.stdout

def sql_rows(sql):
    out=sh([
        "mariadb","asterisk",
        "-N","-B",
        "-e",sql
    ])

    rows=[]
    for line in out.splitlines():
        if line.strip():
            rows.append(line.split("\t"))
    return rows

def sql_exec(sql):
    sh([
        "mariadb","asterisk",
        "-e",sql
    ])

def esc(v):
    return str(v).replace("\\","\\\\").replace("'","''")

def digits(v):
    return re.sub(r"\D","",str(v or ""))

def last10(v):
    d=digits(v)
    return d[-10:]

def get_json(url, method="GET"):
    req=urllib.request.Request(
        url,
        data=b"" if method=="POST" else None,
        method=method
    )
    with urllib.request.urlopen(req,timeout=30) as r:
        return json.loads(r.read().decode())

def wav_duration(data):
    try:
        if len(data)<44:
            return 0

        if data[0:4] != b"RIFF":
            return 0

        byte_rate=int.from_bytes(
            data[28:32],
            "little"
        )

        if not byte_rate:
            return 0

        return round(
            max(0,len(data)-44)
            /
            byte_rate
        )
    except:
        return 0

print("="*68)
print("STRINGEE BACKFILL HOY -> LEADSTUDIO CRM")
print("SIN TELEGRAM / SIN WF9 / SIN CREAR FOLLOWUPS")
print("="*68)

# ------------------------------------------------------------
# TOKEN
# ------------------------------------------------------------

tok=get_json(TOKEN_URL,"POST")
TOKEN=tok.get("accessToken","")

if not TOKEN:
    raise SystemExit("ERROR: token LeadStudio ausente")

print("TOKEN=OK")


# ------------------------------------------------------------
# PENDIENTES STRINGEE
# ------------------------------------------------------------

pending=sql_rows("""
SELECT
    phone,
    lead_id,
    followup_id,
    DATE_FORMAT(created_at,'%Y-%m-%d %H:%i:%s')
FROM wf_call_followups
WHERE provider='stringee'
  AND call_status='ANSWERED'
  AND COALESCE(recording_synced,0)=0
  AND created_at >= UTC_DATE()
ORDER BY created_at ASC;
""")

print("PENDIENTES_DB =",len(pending))

if not pending:
    print("Nada pendiente.")
    sys.exit(0)


# ------------------------------------------------------------
# LISTA REAL DEL WORKER STRINGEE
# ------------------------------------------------------------

listing=get_json(LIST_URL)

recordings=listing.get("recordings",[])

print("RECORDINGS_WORKER =",len(recordings))


# Normalizar recordings del día.
worker=[]

for r in recordings:

    try:
        ts=float(r.get("timestamp_ms",0))/1000.0

        dt=datetime.fromtimestamp(
            ts,
            tz=timezone.utc
        )

    except:
        continue

    if dt.strftime("%Y-%m-%d") != TODAY:
        continue

    worker.append({
        "raw":r,
        "filename":str(r.get("filename","")),
        "phone":str(r.get("phone","")),
        "phone10":last10(r.get("phone","")),
        "ts":ts,
        "dt":dt,
    })

print("RECORDINGS_HOY =",len(worker))


# ------------------------------------------------------------
# MATCH SEGURO POR PHONE + HORA
# ------------------------------------------------------------

used=set()
jobs=[]

for phone,lead_id,fid,created_at in pending:

    try:
        fdt=datetime.strptime(
            created_at,
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)

        fts=fdt.timestamp()

    except:
        print(
            "SIN_MATCH fecha invalida",
            phone,
            fid
        )
        continue

    p10=last10(phone)

    candidates=[]

    for r in worker:

        if not r["filename"]:
            continue

        if r["filename"] in used:
            continue

        if r["phone10"] != p10:
            continue

        delta=abs(
            fts-r["ts"]
        )

        # Margen amplio para post-call / ingest.
        if delta <= 1800:
            candidates.append(
                (delta,r)
            )

    if not candidates:
        print(
            "SIN_MATCH",
            phone,
            fid,
            created_at
        )
        continue

    candidates.sort(
        key=lambda x:x[0]
    )

    delta,rec=candidates[0]

    used.add(
        rec["filename"]
    )

    jobs.append({
        "phone":phone,
        "lead_id":lead_id,
        "followup_id":fid,
        "created_at":created_at,
        "recording":rec,
        "delta":round(delta)
    })


print()
print("MATCH_SEGURO =",len(jobs))
print("SIN_MATCH    =",len(pending)-len(jobs))


# ------------------------------------------------------------
# SUBIR UNO POR UNO
# ------------------------------------------------------------

ok=0
fail=0

for i,j in enumerate(jobs,1):

    phone=j["phone"]
    fid=j["followup_id"]
    rec=j["recording"]
    filename=rec["filename"]

    print()
    print(
        f"[{i}/{len(jobs)}]",
        phone,
        filename,
        "delta="+str(j["delta"])+"s"
    )

    try:

        encoded_name=urllib.parse.quote(
            filename,
            safe=""
        )

        dl=get_json(
            LIST_URL+"/"+encoded_name
        )

        if not dl.get("ok"):
            raise RuntimeError(
                "worker devolvio ok=false"
            )

        b64=dl.get("audio_base64")

        if not b64:
            raise RuntimeError(
                "audio_base64 ausente"
            )

        audio=base64.b64decode(b64)

        if len(audio)<=44:
            raise RuntimeError(
                "WAV vacio"
            )

        secs=wav_duration(audio)

        clean_phone=digits(phone)

        clean_name=(
            f"STRINGEE_{clean_phone}_"
            f"{TODAY}_{secs}s.wav"
        )

        tmp="/tmp/"+clean_name

        with open(tmp,"wb") as f:
            f.write(audio)

        try:

            p=subprocess.run(
                [
                    "curl",
                    "-sS",
                    "--connect-timeout","10",
                    "--max-time","90",
                    "-X","PUT",
                    f"{BASE}/api/calls/recording/{fid}",
                    "-H",
                    "Authorization: Bearer "+TOKEN,
                    "-F",
                    f"file=@{tmp};filename={clean_name};type=audio/wav",
                    "-w",
                    "\nHTTP:%{http_code}"
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=100
            )

        finally:
            try:
                os.remove(tmp)
            except:
                pass

        if p.returncode != 0:
            raise RuntimeError(
                "curl: "+p.stderr.strip()
            )

        lines=p.stdout.rstrip().splitlines()

        status=lines[-1] if lines else ""
        body="\n".join(lines[:-1])

        if status not in (
            "HTTP:200",
            "HTTP:201",
            "HTTP:204"
        ):
            raise RuntimeError(
                status+" "+body[:300]
            )

        confirmed=True

        if body.strip():

            try:
                response=json.loads(body)

                if (
                    "hasRecording" in response
                    and
                    response.get("hasRecording") is not True
                ):
                    confirmed=False

                returned=(
                    response.get("followUpId")
                    or
                    response.get("followupId")
                )

                if returned and returned != fid:
                    confirmed=False

            except json.JSONDecodeError:
                pass

        if not confirmed:
            raise RuntimeError(
                "LeadStudio no confirmo el followup"
            )

        efid=esc(fid)

        sql_exec(f"""
UPDATE wf_call_followups
SET recording_synced=1
WHERE followup_id='{efid}'
  AND provider='stringee'
  AND call_status='ANSWERED';
""")

        verify=sql_rows(f"""
SELECT COALESCE(recording_synced,0)
FROM wf_call_followups
WHERE followup_id='{efid}'
LIMIT 1;
""")

        if not verify or verify[0][0]!="1":
            raise RuntimeError(
                "recording_synced no quedo en 1"
            )

        print(
            "CRM_OK",
            fid,
            f"{secs}s"
        )

        ok+=1

    except Exception as e:

        print(
            "CRM_FAIL",
            fid,
            str(e)
        )

        fail+=1


# ------------------------------------------------------------
# RESULTADO
# ------------------------------------------------------------

remaining=sql_rows("""
SELECT COUNT(*)
FROM wf_call_followups
WHERE provider='stringee'
  AND call_status='ANSWERED'
  AND COALESCE(recording_synced,0)=0
  AND created_at >= UTC_DATE();
""")

left=remaining[0][0] if remaining else "?"

print()
print("="*68)
print("RESULTADO FINAL STRINGEE")
print("="*68)
print("PENDIENTES INICIALES :",len(pending))
print("MATCH                 :",len(jobs))
print("SUBIDOS CRM           :",ok)
print("FALLIDOS              :",fail)
print("SIN MATCH              :",len(pending)-len(jobs))
print("PENDIENTES FINALES    :",left)
print()
print("TELEGRAM ENVIADO      : NO")
print("FOLLOWUPS CREADOS     : NO")
print("CLIENTES LLAMADOS     : NO")
print("="*68)
