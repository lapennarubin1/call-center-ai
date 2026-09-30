#!/usr/bin/env python3

import base64
import json
import os
import re
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timezone

TARGET_DATE="2026-09-25"

BASE="https://lead-studio-9gnl.onrender.com"
RECORDINGS="http://172.18.0.1:8091/recordings"
TOKEN_URL="http://172.18.0.1:8092/token"


def sql_rows(sql):
    p=subprocess.run(
        ["mariadb","asterisk","-N","-B","-e",sql],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())

    return [
        x.split("\t")
        for x in p.stdout.splitlines()
        if x.strip()
    ]


def sql_exec(sql):
    p=subprocess.run(
        ["mariadb","asterisk","-e",sql],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())


def get_json(url,method="GET"):
    req=urllib.request.Request(
        url,
        data=b"" if method=="POST" else None,
        method=method
    )

    with urllib.request.urlopen(req,timeout=30) as r:
        return json.loads(r.read().decode())


def digits(v):
    return re.sub(r"\D","",str(v or ""))


def last10(v):
    return digits(v)[-10:]


def esc(v):
    return str(v).replace("\\","\\\\").replace("'","''")


def wav_duration(data):
    try:
        if len(data)<44 or data[:4] != b"RIFF":
            return 0

        byte_rate=int.from_bytes(data[28:32],"little")

        if not byte_rate:
            return 0

        return round((len(data)-44)/byte_rate)

    except:
        return 0


print("="*70)
print("STRINGEE 2026-09-25 -> CRM")
print("SIN TELEGRAM / SIN WF10 / SIN CREAR FOLLOWUPS")
print("="*70)


# ---------------------------------------------------------
# TOKEN
# ---------------------------------------------------------

TOKEN=get_json(TOKEN_URL,"POST").get("accessToken","")

if not TOKEN:
    raise SystemExit("ERROR: token LeadStudio ausente")

print("TOKEN = OK")


# ---------------------------------------------------------
# 15 FOLLOWUPS PENDIENTES
# ---------------------------------------------------------

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
  AND created_at >= '2026-09-25 00:00:00'
  AND created_at <  '2026-09-26 00:00:00'
ORDER BY created_at;
""")

print("PENDIENTES =",len(pending))


# ---------------------------------------------------------
# RECORDINGS STRINGEE
# ---------------------------------------------------------

data=get_json(RECORDINGS)
all_recordings=data.get("recordings",[])

recordings=[]

for r in all_recordings:

    try:
        ts=float(r.get("timestamp_ms",0))/1000

        dt=datetime.fromtimestamp(
            ts,
            tz=timezone.utc
        )

    except:
        continue

    if dt.strftime("%Y-%m-%d") != TARGET_DATE:
        continue

    recordings.append({
        "filename":str(r.get("filename","")),
        "phone":str(r.get("phone","")),
        "phone10":last10(r.get("phone","")),
        "ts":ts
    })


print("RECORDINGS DEL 25 =",len(recordings))


# ---------------------------------------------------------
# MATCH PHONE + TIEMPO
# ---------------------------------------------------------

used=set()
jobs=[]
no_match=[]


for phone,lead_id,fid,created_at in pending:

    fdt=datetime.strptime(
        created_at,
        "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)

    fts=fdt.timestamp()
    p10=last10(phone)

    candidates=[]

    for rec in recordings:

        if rec["filename"] in used:
            continue

        if rec["phone10"] != p10:
            continue

        delta=abs(fts-rec["ts"])

        # máximo 30 minutos de diferencia
        if delta <= 1800:
            candidates.append((delta,rec))


    if not candidates:
        no_match.append((phone,fid,created_at))
        continue


    candidates.sort(key=lambda x:x[0])

    delta,rec=candidates[0]

    used.add(rec["filename"])

    jobs.append({
        "phone":phone,
        "lead_id":lead_id,
        "followup_id":fid,
        "created_at":created_at,
        "filename":rec["filename"],
        "delta":round(delta)
    })


print("MATCH =",len(jobs))
print("SIN MATCH =",len(no_match))


# ---------------------------------------------------------
# UPLOAD
# ---------------------------------------------------------

uploaded=0
failed=0


for n,j in enumerate(jobs,1):

    phone=j["phone"]
    fid=j["followup_id"]
    filename=j["filename"]

    print()
    print(
        f"[{n}/{len(jobs)}]",
        phone,
        fid,
        filename,
        f"delta={j['delta']}s"
    )


    try:

        url=(
            RECORDINGS
            +"/"
            +urllib.parse.quote(filename,safe="")
        )

        dl=get_json(url)

        if not dl.get("ok"):
            raise RuntimeError("worker devolvio ok=false")

        b64=dl.get("audio_base64")

        if not b64:
            raise RuntimeError("audio_base64 ausente")

        audio=base64.b64decode(b64)

        if len(audio)<=44:
            raise RuntimeError("WAV vacio")

        secs=wav_duration(audio)

        clean_phone=digits(phone)

        clean_name=(
            f"STRINGEE_{clean_phone}_"
            f"2026-09-25_{secs}s.wav"
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
            raise RuntimeError(p.stderr.strip())


        parts=p.stdout.rstrip().splitlines()

        http=parts[-1] if parts else ""
        body="\n".join(parts[:-1])


        if http not in (
            "HTTP:200",
            "HTTP:201",
            "HTTP:204"
        ):
            raise RuntimeError(
                http+" "+body[:300]
            )


        if body.strip():

            try:
                result=json.loads(body)

                if (
                    "hasRecording" in result
                    and result.get("hasRecording") is not True
                ):
                    raise RuntimeError(
                        "hasRecording != true"
                    )

                returned=(
                    result.get("followUpId")
                    or result.get("followupId")
                )

                if returned and returned != fid:
                    raise RuntimeError(
                        "followUpId devuelto no coincide"
                    )

            except json.JSONDecodeError:
                pass


        sql_exec(f"""
UPDATE wf_call_followups
SET recording_synced=1
WHERE followup_id='{esc(fid)}'
  AND provider='stringee'
  AND call_status='ANSWERED';
""")


        check=sql_rows(f"""
SELECT COALESCE(recording_synced,0)
FROM wf_call_followups
WHERE followup_id='{esc(fid)}'
LIMIT 1;
""")


        if not check or check[0][0]!="1":
            raise RuntimeError(
                "recording_synced no quedo en 1"
            )


        print(
            "CRM_OK",
            f"{secs}s"
        )

        uploaded+=1


    except Exception as e:

        print(
            "CRM_FAIL:",
            str(e)
        )

        failed+=1


# ---------------------------------------------------------
# FINAL
# ---------------------------------------------------------

remaining=sql_rows("""
SELECT COUNT(*)
FROM wf_call_followups
WHERE provider='stringee'
  AND call_status='ANSWERED'
  AND COALESCE(recording_synced,0)=0
  AND created_at >= '2026-09-25 00:00:00'
  AND created_at <  '2026-09-26 00:00:00';
""")


print()
print("="*70)
print("RESULTADO FINAL")
print("="*70)

print("PENDIENTES INICIALES :",len(pending))
print("MATCH                 :",len(jobs))
print("SUBIDOS CRM           :",uploaded)
print("FALLIDOS              :",failed)
print("SIN MATCH              :",len(no_match))
print("PENDIENTES FINALES    :",remaining[0][0])

print()
print("TELEGRAM               : NO")
print("FOLLOWUPS NUEVOS       : NO")
print("LLAMADAS NUEVAS        : NO")

if no_match:

    print()
    print("SIN MATCH:")

    for x in no_match:
        print(*x)

