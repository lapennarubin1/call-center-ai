"""
Genera una BD SQLite que replica EXACTAMENTE el esquema del CDR de Asterisk
y las tablas del panel, con datos realistas del sistema Landmark Markets.

Volúmenes reales observados (03-04/08/2026):
  - 2391 llamadas el 03/08, 349 contestadas (14.6%)
  - 654 llamadas el 04/08, 84 contestadas (12.8%)
  - src siempre '650098' (el user del trunk proveedor1)
  - dst formato '+91XXXXXXXXXX'
  - Ventana horaria 11:00-19:00 IST = 05:30-13:30 UTC
"""
import sqlite3, random, os
from datetime import datetime, timedelta

DB = os.path.join(os.path.dirname(__file__), 'mock_asterisk.db')
if os.path.exists(DB):
    os.remove(DB)

conn = sqlite3.connect(DB)
c = conn.cursor()

# ── Esquema CDR de Asterisk (idéntico al real) ──────────────────────
c.execute("""
CREATE TABLE cdr (
    calldate    DATETIME NOT NULL DEFAULT '0000-00-00 00:00:00',
    clid        VARCHAR(80)  NOT NULL DEFAULT '',
    src         VARCHAR(80)  NOT NULL DEFAULT '',
    dst         VARCHAR(80)  NOT NULL DEFAULT '',
    dcontext    VARCHAR(80)  NOT NULL DEFAULT '',
    channel     VARCHAR(80)  NOT NULL DEFAULT '',
    dstchannel  VARCHAR(80)  NOT NULL DEFAULT '',
    lastapp     VARCHAR(80)  NOT NULL DEFAULT '',
    lastdata    VARCHAR(80)  NOT NULL DEFAULT '',
    duration    INT NOT NULL DEFAULT 0,
    billsec     INT NOT NULL DEFAULT 0,
    disposition VARCHAR(45)  NOT NULL DEFAULT '',
    amaflags    INT NOT NULL DEFAULT 0,
    accountcode VARCHAR(20)  NOT NULL DEFAULT '',
    uniqueid    VARCHAR(32)  NOT NULL DEFAULT '',
    userfield   VARCHAR(255) NOT NULL DEFAULT ''
)""")

# ── Tablas del panel (sincronizadas desde Google Sheets por WF14) ───
c.execute("""
CREATE TABLE panel_leads (
    lead_id          VARCHAR(64) PRIMARY KEY,
    full_name        VARCHAR(160),
    phone            VARCHAR(32),
    country          VARCHAR(64),
    language         VARCHAR(16),
    status           VARCHAR(48),
    last_call_status VARCHAR(48),
    call_attempts    INT DEFAULT 0,
    last_call_time   DATETIME,
    duration_secs    INT DEFAULT 0,
    synced_at        DATETIME
)""")

c.execute("""
CREATE TABLE panel_conversions (
    lead_id       VARCHAR(64) PRIMARY KEY,
    full_name     VARCHAR(160),
    phone         VARCHAR(32),
    atlantis_user VARCHAR(80),
    account_id    VARCHAR(80),
    created_at    DATETIME,
    synced_at     DATETIME
)""")

c.execute("CREATE INDEX idx_cdr_calldate ON cdr(calldate)")
c.execute("CREATE INDEX idx_cdr_disp ON cdr(disposition)")
c.execute("CREATE INDEX idx_leads_status ON panel_leads(status)")

# ── Generación de datos ────────────────────────────────────────────
random.seed(42)
today = datetime(2026, 8, 6, 14, 0, 0)

# Perfil por día: (dias_atras, total_llamadas, tasa_contestacion)
# Replica el patrón real: días fuertes ~2400, días flojos, fines de semana bajos
day_profiles = []
for d in range(0, 400):
    date = today - timedelta(days=d)
    if date.weekday() == 6:          # domingo: no se opera
        total = 0
    elif date.weekday() == 5:        # sábado: medio día
        total = random.randint(600, 1100)
    else:
        total = random.randint(1600, 2600)
    # tasa de contestación varía 9%-16%
    rate = random.uniform(0.09, 0.16)
    day_profiles.append((d, total, rate))

rows = []
lead_counter = 20000
conversions = []
leads = []

for d, total, rate in day_profiles:
    day = today - timedelta(days=d)
    if total == 0:
        continue
    answered_target = int(total * rate)
    answered_so_far = 0

    for i in range(total):
        # Distribución horaria: 05:30-13:30 UTC (11:00-19:00 IST)
        minute_offset = random.randint(0, 8 * 60)
        call_dt = day.replace(hour=5, minute=30, second=0, microsecond=0) + timedelta(minutes=minute_offset)
        if d == 0 and call_dt > today:
            continue

        phone = '+91' + str(random.randint(6000000000, 9999999999))

        # Decidir disposición
        r = random.random()
        if answered_so_far < answered_target and r < rate * 1.15:
            disposition = 'ANSWERED'
            answered_so_far += 1
            # ALOC realista: mayoría cortas, cola larga de conversaciones reales
            if random.random() < 0.55:
                billsec = random.randint(3, 25)      # cuelgan rápido
            elif random.random() < 0.75:
                billsec = random.randint(26, 90)
            else:
                billsec = random.randint(91, 320)    # conversación real
            duration = billsec + random.randint(6, 30)
        elif r < 0.955:
            disposition = 'NO ANSWER'
            billsec = 0
            duration = random.randint(8, 62)
        elif r < 0.985:
            disposition = 'FAILED'
            billsec = 0
            duration = random.randint(0, 4)
        elif r < 0.995:
            disposition = 'BUSY'
            billsec = 0
            duration = random.randint(1, 8)
        else:
            disposition = 'CONGESTION'
            billsec = 0
            duration = random.randint(0, 3)

        rows.append((
            call_dt.strftime('%Y-%m-%d %H:%M:%S'),
            '"India" <650098>', '650098', phone, 'from-elevenlabs',
            'PJSIP/elevenlabs-0000', 'PJSIP/proveedor1-0000',
            'Dial', f'PJSIP/{phone}@proveedor1',
            duration, billsec, disposition, 3, '',
            f'{int(call_dt.timestamp())}.{i}', ''
        ))

# ── Leads en el sheet (replicando la taxonomía real) ────────────────
status_mix = [
    ('NO_ANSWER',        0.62),
    ('SUCCESSFUL',       0.13),
    ('FAILED',           0.09),
    ('VOICEMAIL',        0.04),
    ('READY_TO_CALL',    0.06),
    ('CALL_IN_PROGRESS', 0.01),
    ('SCHEDULED',        0.015),
    ('INTERESTED',       0.012),
    ('CONVERTED',        0.008),
    ('DO_NOT_CALL',      0.005),
]
statuses, weights = zip(*status_mix)

for i in range(15000):
    lead_counter += 1
    st = random.choices(statuses, weights=weights)[0]
    days_ago = random.randint(0, 120)
    lct = today - timedelta(days=days_ago, minutes=random.randint(0, 500))
    dur = random.randint(20, 300) if st in ('SUCCESSFUL', 'INTERESTED', 'CONVERTED') else 0
    leads.append((
        str(lead_counter),
        random.choice(['', '', 'Aadhu Kotaka', 'Lalitun Mukhi', 'Mohammad Rafee', 'Satish Kumar']),
        '91' + str(random.randint(6000000000, 9999999999)),
        'India', 'hi', st,
        {'SUCCESSFUL': 'COMPLETED', 'NO_ANSWER': 'NO_ANSWER', 'FAILED': 'NO_CONV_ID',
         'VOICEMAIL': 'VOICEMAIL', 'INTERESTED': 'COMPLETED', 'CONVERTED': 'COMPLETED'}.get(st, ''),
        random.randint(0, 3),
        lct.strftime('%Y-%m-%d %H:%M:%S'),
        dur,
        today.strftime('%Y-%m-%d %H:%M:%S')
    ))
    # Los INTERESTED y CONVERTED tienen cuenta abierta
    if st in ('INTERESTED', 'CONVERTED'):
        conversions.append((
            str(lead_counter),
            'Cliente ' + str(lead_counter),
            '91' + str(random.randint(6000000000, 9999999999)),
            f'user{lead_counter}',
            f'acct{lead_counter}',
            lct.strftime('%Y-%m-%d %H:%M:%S'),
            today.strftime('%Y-%m-%d %H:%M:%S')
        ))

c.executemany("INSERT INTO cdr VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
c.executemany("INSERT INTO panel_leads VALUES (?,?,?,?,?,?,?,?,?,?,?)", leads)
c.executemany("INSERT INTO panel_conversions VALUES (?,?,?,?,?,?,?)", conversions)
conn.commit()

print(f"CDR rows        : {len(rows):,}")
print(f"Leads           : {len(leads):,}")
print(f"Conversions     : {len(conversions):,}")
c.execute("SELECT COUNT(*), SUM(disposition='ANSWERED') FROM cdr WHERE DATE(calldate)=DATE('2026-08-05')")
t, a = c.fetchone()
print(f"Muestra 05/08   : {t} llamadas, {a} contestadas ({a/t*100:.1f}%)")
conn.close()
print(f"\nBD creada: {DB}")
