"""Base de prueba con datos sintéticos CONTROLADOS.
Los datos de 'hoy' usan solo horas UTC tempranas (0-3) para que siempre
estén en el pasado sin importar cuándo se corra el test. La verificación
de conversión de husos usa AYER, que siempre es un día completo."""
import sqlite3, os, sys
from datetime import datetime, timedelta, timezone

def build(path):
    if os.path.exists(path): os.remove(path)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE cdr (calldate DATETIME, disposition TEXT, billsec INTEGER, dst TEXT, channel TEXT)")
    c.execute("CREATE TABLE panel_leads (status TEXT, last_call_time DATETIME, country TEXT, synced_at DATETIME)")
    c.execute("CREATE TABLE panel_conversions (lead_id TEXT, full_name TEXT, phone TEXT, atlantis_user TEXT, account_id TEXT, created_at DATETIME, country TEXT)")
    f = '%Y-%m-%d %H:%M:%S'
    hoy = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    ayer = hoy - timedelta(days=1)
    def cdr(dt, disp, bs, dst, ch='PJSIP/x-1'):
        c.execute("INSERT INTO cdr VALUES (?,?,?,?,?)", (dt.strftime(f), disp, bs, dst, ch))
    def conv(lid, dt, country):
        c.execute("INSERT INTO panel_conversions VALUES (?,?,?,?,?,?,?)", (lid,'N','+1','u','a', dt.strftime(f), country))

    IN, MX = '+919812345678', '+525512345678'

    # ══ HOY (horas UTC 0-3, siempre pasadas) ══
    # India hoy: 10 llamadas / 6 contestadas / minutos = 3*1+2*2+1*4 = 11
    for i in range(3): cdr(hoy+timedelta(hours=0,minutes=i), 'ANSWERED', 60, IN)
    for i in range(2): cdr(hoy+timedelta(hours=1,minutes=i), 'ANSWERED', 90, IN)
    cdr(hoy+timedelta(hours=2), 'ANSWERED', 200, IN)
    for i in range(4): cdr(hoy+timedelta(hours=3,minutes=i), 'NO ANSWER', 0, IN)
    # Mexico hoy: 4 llamadas / 2 contestadas / 2 min
    for i in range(2): cdr(hoy+timedelta(hours=0,minutes=10+i), 'ANSWERED', 60, MX)
    for i in range(2): cdr(hoy+timedelta(hours=1,minutes=10+i), 'BUSY', 0, MX)
    # Cuentas hoy: India 3, Mexico 1
    for i in range(3): conv(f'L{i}', hoy+timedelta(hours=2), 'India')
    conv('L9', hoy+timedelta(hours=2), 'Mexico')

    # ══ AYER — día completo, para verificar conversión de husos ══
    # India: UTC 5 (x3), UTC 6 (x2), UTC 7 (x1 answered), UTC 8 (x4 no answer)
    #   IST +5:30 => UTC5->10, UTC6->11, UTC7->12, UTC8->13
    for i in range(3): cdr(ayer+timedelta(hours=5,minutes=i), 'ANSWERED', 60, IN)
    for i in range(2): cdr(ayer+timedelta(hours=6,minutes=i), 'ANSWERED', 60, IN)
    cdr(ayer+timedelta(hours=7), 'ANSWERED', 60, IN)
    for i in range(4): cdr(ayer+timedelta(hours=8,minutes=i), 'NO ANSWER', 0, IN)
    conv('LA', ayer+timedelta(hours=9), 'India')
    # Mexico ayer: UTC 20 (x2), UTC 21 (x2)  =>  CST -6: 14 y 15
    for i in range(2): cdr(ayer+timedelta(hours=20,minutes=i), 'ANSWERED', 60, MX)
    for i in range(2): cdr(ayer+timedelta(hours=21,minutes=i), 'ANSWERED', 60, MX)

    # ══ LEADS India: 5 NEW, 3 CALLED, 2 FAILED ══
    for st, n in [('NEW',5), ('CALLED',3), ('FAILED',2)]:
        for i in range(n):
            c.execute("INSERT INTO panel_leads VALUES (?,?,?,?)", (st, hoy.strftime(f), 'India', hoy.strftime(f)))

    c.commit(); c.close()
    return path

if __name__ == '__main__':
    print(build(sys.argv[1] if len(sys.argv)>1 else '/tmp/jtest.db'))
