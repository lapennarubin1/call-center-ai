#!/usr/bin/env python3
"""
Generador de configuracion de Asterisk desde la base de datos.

Lee providers, connections y clients y escribe:
    /etc/asterisk/pjsip_gen.conf
    /etc/asterisk/extensions_gen.conf
Luego (opcional) recarga Asterisk.

Asi TODO lo que editas en el panel (proveedores, clientes, conexiones)
se refleja en la config real con un clic ("Aplicar y recargar").

En /etc/asterisk/pjsip.conf y extensions.conf deja SOLO:
    #include "pjsip_gen.conf"
    #include "extensions_gen.conf"
(mas la parte fija: transports, globals, subrutinas si prefieres fijas).
"""
import subprocess
import sys

try:
    import pymysql
except ImportError:
    sys.exit("Instala PyMySQL:  pip install pymysql")

DB = dict(host="127.0.0.1", user="asterisk_ro",
          password="CAMBIA_ESTO_PASS_BD", database="asterisk",
          cursorclass=pymysql.cursors.DictCursor)

PJSIP_OUT = "/etc/asterisk/pjsip_gen.conf"
EXTEN_OUT = "/etc/asterisk/extensions_gen.conf"


def q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()


def gen_pjsip(cur):
    out = [";; ARCHIVO GENERADO AUTOMATICAMENTE - NO EDITAR A MANO ;;\n"]

    # ---- PROVEEDORES ----
    for p in q(cur, "SELECT * FROM providers WHERE active=1 ORDER BY priority"):
        name = p["name"]
        tp = f"transport-{p['transport']}"
        if p["auth_type"] == "userpass" and p["username"]:
            out.append(f"""
[{name}-reg]
type=registration
transport={tp}
outbound_auth={name}-auth
server_uri=sip:{p['host']}
client_uri=sip:{p['username']}@{p['host']}
retry_interval=60
forbidden_retry_interval=300
expiration=3600
line=yes
endpoint={name}

[{name}-auth]
type=auth
auth_type=userpass
username={p['username']}
password={p['password']}""")

        out.append(f"""
[{name}-aor]
type=aor
contact=sip:{p['host']}:{p['port']}
qualify_frequency=60

[{name}]
type=endpoint
transport={tp}
context=from-provider
disallow=all
allow={p['codecs']}
aors={name}-aor
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes""")
        if p["auth_type"] == "userpass" and p["username"]:
            out.append(f"outbound_auth={name}-auth")
        if p["from_domain"]:
            out.append(f"from_domain={p['from_domain']}")
        if p["ip_cidrs"]:
            out.append(f"\n[{name}-identify]\ntype=identify\nendpoint={name}")
            for cidr in p["ip_cidrs"].split(","):
                cidr = cidr.strip()
                if cidr:
                    out.append(f"match={cidr}")

    # ---- CONEXIONES DE CLIENTE ----
    for c in q(cur, "SELECT * FROM connections WHERE active=1"):
        u = c["username"]
        tp = f"transport-{c['transport']}"
        endpoint = f"""
[{u}]
type=endpoint
transport={tp}
context=from-client-{c['client']}
disallow=all
allow=ulaw,alaw
auth={u}-auth
aors={u}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes"""
        if c["transport"] == "tls":
            endpoint += "\nmedia_encryption=sdes\nmedia_encryption_optimistic=no"
        out.append(endpoint)
        out.append(f"""
[{u}-auth]
type=auth
auth_type=userpass
username={u}
password={c['password']}

[{u}]
type=aor
max_contacts=3
qualify_frequency=60
remove_existing=yes""")

    return "\n".join(out) + "\n"


def gen_extensions(cur):
    out = [";; ARCHIVO GENERADO AUTOMATICAMENTE - NO EDITAR A MANO ;;\n"]

    # Orden LCR por defecto = proveedores activos por prioridad
    provs = q(cur, "SELECT name FROM providers WHERE active=1 ORDER BY priority")
    default_order = ",".join(p["name"] for p in provs) or "telnyx"

    for cl in q(cur, "SELECT * FROM clients"):
        client = cl["client"]
        maxch = cl["max_channels"] or 10
        # Orden de proveedores: si alguna conexion define override, se usa esa;
        # como es a nivel cliente, tomamos el override de la primera conexion que lo tenga.
        conns = q(cur, "SELECT provider_order FROM connections WHERE client=%s", (client,))
        order = default_order
        for cc in conns:
            if cc["provider_order"]:
                order = cc["provider_order"]
                break

        out.append(f"""
[from-client-{client}]
exten => _X.,1,NoOp(== Saliente {client} -> ${{EXTEN}} ==)
 same => n,Set(CLIENT={client})
 same => n,Set(MAXCHANS={maxch})
 same => n,Set(PROVIDERS={order})
 same => n,Set(CDR(accountcode)=${{CLIENT}})
 same => n,Set(CHANNEL(accountcode)=${{CLIENT}})
 same => n,GotoIf($["${{ODBC_CLIENT_ACTIVE(${{CLIENT}})}}"="1"]?activo)
 same => n,Hangup(21)
 same => n(activo),Gosub(sub-fraudcheck,s,1(${{EXTEN}}))
 same => n,GotoIf($["${{GOSUB_RETVAL}}"="1"]?blocked)
 same => n,Gosub(sub-chanlimit,s,1(${{CLIENT}},${{MAXCHANS}}))
 same => n,GotoIf($["${{GOSUB_RETVAL}}"="1"]?limit)
 same => n,Set(CALLERID(num)=${{ODBC_DID_NEXT(${{CLIENT}})}})
 same => n,GotoIf($["${{CALLERID(num)}}"=""]?nodid)
 same => n,Set(CALLERID(name)=${{CALLERID(num)}})
 same => n,Gosub(sub-dialout,s,1(${{EXTEN}},${{PROVIDERS}}))
 same => n,Hangup()
 same => n(blocked),Hangup(21)
 same => n(limit),Playback(all-circuits-busy-now)
 same => n,Hangup(34)
 same => n(nodid),Hangup(21)""")

    return "\n".join(out) + "\n"


def main(reload=True):
    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    pj = gen_pjsip(cur)
    ex = gen_extensions(cur)
    cur.close()
    conn.close()

    with open(PJSIP_OUT, "w") as f:
        f.write(pj)
    with open(EXTEN_OUT, "w") as f:
        f.write(ex)
    print(f"Generados {PJSIP_OUT} y {EXTEN_OUT}")

    if reload:
        try:
            subprocess.run(["asterisk", "-rx", "pjsip reload"], check=False)
            subprocess.run(["asterisk", "-rx", "dialplan reload"], check=False)
            print("Asterisk recargado.")
        except FileNotFoundError:
            print("Asterisk no encontrado en PATH; recarga manual: asterisk -rx 'core reload'")


if __name__ == "__main__":
    main(reload="--no-reload" not in sys.argv)
