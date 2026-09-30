#!/usr/bin/env python3
"""
Motor de tarificacion (rating) de la plataforma ITSP.

Toma los CDR aun no facturados, calcula el importe segun el plan de
tarifas del cliente (match de prefijo mas largo + incremento de
facturacion) y crea el registro en billing_records. Si el cliente es
prepago, descuenta del saldo y lo desactiva si se queda sin fondos.

Uso:
    python3 rating.py            # tarifica lo pendiente
Puede correr por cron cada 1-5 min, o llamarse desde el panel.
"""
import math
import sys

try:
    import pymysql
except ImportError:
    sys.exit("Instala PyMySQL:  pip install pymysql")

DB = dict(host="127.0.0.1", user="asterisk_rw",
          password="CAMBIA_ESTO_PASS_BD_RW", database="asterisk",
          cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def parse_provider(dstchannel):
    """De 'PJSIP/155512345@telnyx-0000abcd' saca 'telnyx'."""
    if not dstchannel or "@" not in dstchannel:
        return None
    tail = dstchannel.split("@", 1)[1]
    return tail.rsplit("-", 1)[0] if "-" in tail else tail


def longest_prefix(cur, table, key_col, key_val, dst):
    """Devuelve la fila con el prefijo mas largo que es prefijo de dst."""
    cur.execute(
        f"SELECT * FROM {table} WHERE {key_col}=%s ORDER BY LENGTH(prefix) DESC",
        (key_val,))
    for row in cur.fetchall():
        if dst.startswith(row["prefix"]):
            return row
    return None


def billed_seconds(billsec, min_seconds, increment):
    inc = max(1, increment)
    dur = max(billsec, min_seconds)
    return int(math.ceil(dur / inc) * inc)


def rate_pending():
    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    processed = 0

    # CDRs contestados, con duracion, aun no facturados
    cur.execute("""
        SELECT c.id, c.dst, c.billsec, c.accountcode, c.dstchannel
        FROM cdr c
        LEFT JOIN billing_records b ON b.cdr_id = c.id
        WHERE b.id IS NULL
          AND c.disposition = 'ANSWERED'
          AND c.billsec > 0
          AND c.accountcode <> ''
        LIMIT 5000
    """)
    rows = cur.fetchall()

    for r in rows:
        client = r["accountcode"]
        dst = (r["dst"] or "").strip()

        # Plan y moneda del cliente
        cur.execute("SELECT rate_plan_id, currency, billing_type FROM clients WHERE client=%s",
                    (client,))
        cl = cur.fetchone()
        if not cl or not cl["rate_plan_id"]:
            # sin plan asignado: no se puede tarificar -> se salta
            continue
        plan_id = cl["rate_plan_id"]
        currency = cl["currency"] or "USD"

        rate = longest_prefix(cur, "sell_rates", "plan_id", plan_id, dst)
        if not rate:
            # destino sin tarifa en el plan -> registro a 0 para no reintentar
            bsec = r["billsec"]
            cur.execute("""INSERT INTO billing_records
                (cdr_id, client, dst, prefix_matched, plan_id, billsec,
                 billed_seconds, rate_per_min, amount, currency)
                VALUES (%s,%s,%s,NULL,%s,%s,%s,0,0,%s)""",
                (r["id"], client, dst, plan_id, bsec, bsec, currency))
            processed += 1
            continue

        bsec = billed_seconds(r["billsec"], rate["min_seconds"],
                              rate["increment_seconds"])
        minutes = bsec / 60.0
        amount = float(rate["connect_fee"]) + float(rate["rate_per_min"]) * minutes

        # Costo (opcional) segun proveedor usado y buy_rates
        cost = None
        prov = parse_provider(r["dstchannel"])
        if prov:
            brow = longest_prefix(cur, "buy_rates", "provider", prov, dst)
            if brow:
                bcost_sec = billed_seconds(r["billsec"], 0, brow["increment_seconds"])
                cost = float(brow["rate_per_min"]) * (bcost_sec / 60.0)

        cur.execute("""INSERT INTO billing_records
            (cdr_id, client, dst, prefix_matched, plan_id, billsec,
             billed_seconds, rate_per_min, amount, cost, currency)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (r["id"], client, dst, rate["prefix"], plan_id, r["billsec"],
             bsec, rate["rate_per_min"], round(amount, 5),
             None if cost is None else round(cost, 5), currency))
        processed += 1

        # Prepago: descontar saldo y cortar si se agota
        if cl["billing_type"] == "prepaid":
            cur.execute("UPDATE clients SET balance = balance - %s WHERE client=%s",
                        (round(amount, 5), client))
            cur.execute("SELECT balance FROM clients WHERE client=%s", (client,))
            bal = float(cur.fetchone()["balance"])
            if bal <= 0:
                cur.execute("UPDATE clients SET active=0 WHERE client=%s", (client,))
                cur.execute("""INSERT INTO fraud_events (client, rule, detail, action)
                    VALUES (%s,'balance_zero',%s,'client_disabled')""",
                    (client, f"Saldo agotado ({bal:.4f} {currency})"))

    conn.commit()
    cur.close()
    conn.close()
    return processed


if __name__ == "__main__":
    n = rate_pending()
    print(f"Tarificadas {n} llamadas.")
