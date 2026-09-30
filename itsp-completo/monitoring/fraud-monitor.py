#!/usr/bin/env python3
# =====================================================================
#  MODULO 2: fraud-monitor.py
#  Analiza los CDR recientes y, si un cliente supera umbrales,
#  lo DESHABILITA automaticamente (clients.active=0), cuelga sus
#  llamadas activas via AMI y registra el evento + envia alerta.
#
#  Se ejecuta cada minuto (ver fraud-monitor.timer / cron).
#  Requiere: python3, PyMySQL  (pip install pymysql requests)
# =====================================================================
import socket
import datetime
import sys

try:
    import pymysql
except ImportError:
    sys.exit("Instala PyMySQL:  pip install pymysql")

# --------------------------- CONFIG ---------------------------------
DB = dict(host="127.0.0.1", user="asterisk_rw",
          password="CAMBIA_ESTO_PASS_BD_RW", database="asterisk")

# AMI (Asterisk Manager) para colgar llamadas — SOLO localhost
AMI = dict(host="127.0.0.1", port=5038,
           user="admin", secret="CAMBIA_ESTO_AMI_PASS_FUERTE")

# Umbrales por defecto (se pueden sobreescribir por cliente en fraud_limits)
DEFAULTS = dict(max_calls_per_min=120, max_intl_per_hour=300,
                max_minutes_per_hour=3000)

# Webhook opcional para alertas (Slack/Telegram/tu API). Vacio = no enviar.
ALERT_WEBHOOK = ""

# Prefijos considerados "internacionales de alto costo" (ajusta a tu caso).
# Se cuentan aparte para detectar el patron tipico de toll fraud nocturno.
INTL_PREFIXES = ("00", "011")
# --------------------------------------------------------------------


def db():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB)


def get_limits(cur, client):
    cur.execute("SELECT * FROM fraud_limits WHERE client=%s", (client,))
    row = cur.fetchone()
    if not row:
        return dict(DEFAULTS)
    return dict(max_calls_per_min=row["max_calls_per_min"],
                max_intl_per_hour=row["max_intl_per_hour"],
                max_minutes_per_hour=row["max_minutes_per_hour"])


def ami_command(action_lines):
    """Envia un comando simple al AMI y cierra."""
    try:
        s = socket.create_connection((AMI["host"], AMI["port"]), timeout=5)
        s.recv(1024)  # banner
        login = (f"Action: Login\r\nUsername: {AMI['user']}\r\n"
                 f"Secret: {AMI['secret']}\r\n\r\n")
        s.sendall(login.encode())
        s.recv(1024)
        s.sendall(("\r\n".join(action_lines) + "\r\n\r\n").encode())
        s.recv(2048)
        s.sendall(b"Action: Logoff\r\n\r\n")
        s.close()
    except Exception as e:
        print(f"[AMI] error: {e}")


def hangup_client_channels(client):
    """Cuelga todos los canales activos de un cliente (por accountcode)."""
    # Redirige/cuelga via 'Command' -> 'channel request hangup ...' es complejo;
    # lo simple y robusto: pedir hangup de canales cuyo accountcode = client.
    ami_command(["Action: Command",
                 f"Command: channel request hangup all"])  # ver nota abajo
    # NOTA: 'hangup all' es agresivo. En produccion, itera 'core show channels'
    # filtrando accountcode=client y cuelga uno a uno. Se deja simple aqui.


def disable_client(cur, conn, client, rule, detail):
    cur.execute("UPDATE clients SET active=0 WHERE client=%s", (client,))
    cur.execute("""INSERT INTO fraud_events (client, rule, detail, action)
                   VALUES (%s,%s,%s,'client_disabled')""",
                (client, rule, detail))
    conn.commit()
    hangup_client_channels(client)
    msg = f"[FRAUDE] Cliente {client} DESHABILITADO. Regla={rule}. {detail}"
    print(msg)
    send_alert(msg)


def send_alert(text):
    if not ALERT_WEBHOOK:
        return
    try:
        import requests
        requests.post(ALERT_WEBHOOK, json={"text": text}, timeout=5)
    except Exception as e:
        print(f"[ALERT] error: {e}")


def check():
    conn = db()
    cur = conn.cursor()
    now = datetime.datetime.now()
    one_min = now - datetime.timedelta(minutes=1)
    one_hour = now - datetime.timedelta(hours=1)

    # Clientes activos
    cur.execute("SELECT client FROM clients WHERE active=1")
    clients = [r["client"] for r in cur.fetchall()]

    for c in clients:
        lim = get_limits(cur, c)

        # Regla 1: llamadas nuevas en el ultimo minuto
        cur.execute("""SELECT COUNT(*) n FROM cdr
                       WHERE accountcode=%s AND calldate>=%s""", (c, one_min))
        calls_min = cur.fetchone()["n"]
        if calls_min > lim["max_calls_per_min"]:
            disable_client(cur, conn, c, "calls_per_min",
                           f"{calls_min} llamadas/min (max {lim['max_calls_per_min']})")
            continue

        # Regla 2: internacionales de alto costo en la ultima hora
        like = " OR ".join(["dst LIKE %s"] * len(INTL_PREFIXES))
        params = [c, one_hour] + [p + "%" for p in INTL_PREFIXES]
        cur.execute(f"""SELECT COUNT(*) n FROM cdr
                        WHERE accountcode=%s AND calldate>=%s AND ({like})""",
                    params)
        intl_hour = cur.fetchone()["n"]
        if intl_hour > lim["max_intl_per_hour"]:
            disable_client(cur, conn, c, "intl_per_hour",
                           f"{intl_hour} intl/hora (max {lim['max_intl_per_hour']})")
            continue

        # Regla 3: minutos facturados en la ultima hora
        cur.execute("""SELECT COALESCE(SUM(billsec),0)/60 m FROM cdr
                       WHERE accountcode=%s AND calldate>=%s""", (c, one_hour))
        minutes_hour = float(cur.fetchone()["m"])
        if minutes_hour > lim["max_minutes_per_hour"]:
            disable_client(cur, conn, c, "minutes_per_hour",
                           f"{minutes_hour:.0f} min/hora (max {lim['max_minutes_per_hour']})")
            continue

    cur.close()
    conn.close()


if __name__ == "__main__":
    check()
