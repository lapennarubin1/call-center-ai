"""Arnés de los tests del suite V2. Sin dependencias externas.

Reusa el patrón del arnés de la fundación V2.2 (mismos nombres de variables de
entorno) para que las dos baterías se corran juntas con la misma configuración.
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
APP = os.path.join(ROOT, 'panel', 'app')
TOOLS = os.path.join(ROOT, 'tools')
JS_DIR = os.path.join(TOOLS, 'js')
WORKFLOWS = os.path.join(ROOT, 'workflows')
MIGRATION = os.path.join(ROOT, 'sql', 'migration.sql')
ROLLBACK = os.path.join(ROOT, 'sql', 'rollback.sql')

sys.path.insert(0, APP)
sys.path.insert(0, TOOLS)
os.environ.setdefault('LM_SCHEDULER_DISABLED', '1')
os.environ.setdefault('LM_TELEGRAM_DISABLED', '1')
# Sin LM_SECRET el panel se genera una clave de sesión y la ESCRIBE en
# panel/app/.session_key — un secreto dentro del árbol del paquete. Es el
# mismo remedio que documenta INSTALL_STAGING §3, aplicado aquí para que
# correr los tests no ensucie lo que se va a empaquetar.
os.environ.setdefault('LM_SECRET', 'test-only-session-key-not-a-real-secret')
# Y que importar los módulos del panel no deje .pyc dentro del paquete.
sys.dont_write_bytecode = True
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')

MYSQL = {
    'host': os.getenv('LM_TEST_MYSQL_HOST', '127.0.0.1'),
    'user': os.getenv('LM_TEST_MYSQL_USER', 'lmtest'),
    # Credencial de un MariaDB local y desechable que sólo existe
    # mientras corren los tests. No da acceso a nada real.
    # SECSCAN-OK: credencial de test, MariaDB local desechable
    'password': os.getenv('LM_TEST_MYSQL_PASS', 'lmtest'),
    'port': int(os.getenv('LM_TEST_MYSQL_PORT', '3306')),
}


class Suite:
    def __init__(self, name):
        self.name, self.passed, self.failed, self.skipped = name, [], [], []

    def section(self, title):
        print(f"\n── {title} " + '─' * max(4, 62 - len(title)))

    def check(self, name, fn):
        try:
            fn()
            self.passed.append(name)
            print(f"  ok    {name}")
        except AssertionError as ex:
            self.failed.append((name, str(ex)))
            print(f"  FAIL  {name}\n        {ex}")
        except Exception as ex:
            self.failed.append((name, f"{type(ex).__name__}: {ex}"))
            print(f"  ERROR {name}\n        {type(ex).__name__}: {ex}")
            traceback.print_exc(limit=4)

    def skip(self, name, why):
        self.skipped.append((name, why))
        print(f"  skip  {name} ({why})")

    def finish(self):
        print('\n' + '=' * 70)
        print(f"  {self.name}: {len(self.passed)} PASS · {len(self.failed)} FAIL · "
              f"{len(self.skipped)} SKIP")
        for n, e in self.failed:
            print(f"   x {n}: {e}")
        print('=' * 70)
        return 1 if self.failed else 0


# ── MariaDB ───────────────────────────────────────────────────────────
def mysql_available():
    try:
        import pymysql
        c = pymysql.connect(**MYSQL, autocommit=True)
        c.close()
        return True
    except Exception:
        return False


def mysql_conn(db=None, autocommit=True):
    import pymysql
    kw = dict(MYSQL, autocommit=autocommit, charset='utf8mb4')
    if db:
        kw['database'] = db
    return pymysql.connect(**kw)


def fresh_mysql_db(name):
    c = mysql_conn()
    with c.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    c.close()


def split_sql(text):
    """Separa un .sql en sentencias respetando literales entre comillas."""
    out, buf, i, n, in_str = [], [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == '\\' and i + 1 < n:
                buf.append(text[i + 1]); i += 2; continue
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'"); i += 2; continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True; buf.append(ch); i += 1; continue
        if ch == '-' and text.startswith('--', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        if ch == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []; i += 1; continue
        buf.append(ch); i += 1
    tail = ''.join(buf).strip()
    if tail:
        out.append(tail)
    return out


def run_sql_file(dbname, path, limit=None):
    stmts = split_sql(open(path).read())
    c = mysql_conn(dbname)
    with c.cursor() as cur:
        for i, s in enumerate(stmts):
            if limit is not None and i >= limit:
                break
            cur.execute(s)
    c.close()
    return len(stmts)


def migrated_db(name, baseline=False):
    """Base nueva con migration.sql aplicada.

    Desde r2 la migración deja TODO apagado en una instalación limpia
    (países, proveedores y rutas), porque §64 y §77 prohíben encender nada
    en silencio. Un test que ejercite los interruptores tiene que partir de
    algo encendido, así que lo enciende él mismo con `baseline=True` —
    explícito, en vez de depender de lo que sembró la fundación.
    """
    fresh_mysql_db(name)
    run_sql_file(name, MIGRATION)
    if baseline:
        enable_seeded_baseline(name)
    return name


def enable_seeded_baseline(name, mode='V2_PRIMARY'):
    """Deja el sistema en estado operativo: modo V2_PRIMARY, India
    encendida, los dos proveedores y las dos rutas de India.

    Es EXACTAMENTE lo que un operador haría tras instalar: configurar,
    hacer el cutover, y encender. Existe para que los tests de
    interruptores y de capacidad tengan algo que apagar.

    El modo importa: desde r2-final2 la migración deja `LEGACY_BACKUP`,
    y en ese modo ninguna ruta es invocable por mucho que se enciendan
    los tres interruptores. Un test de capacidad que no ponga el modo
    mediría cero y pasaría o fallaría por el motivo equivocado.

    Con `mode=None` el modo se deja EXACTAMENTE como esté: sirve
    para probar lo contrario, que encender los tres interruptores
    no basta para llamar si nadie hizo el cutover.
    """
    c = mysql_conn(name)
    with c.cursor() as cur:
        if mode is not None:
            cur.execute("""INSERT INTO app_settings (setting_key, setting_value)
                           VALUES ('lm_operating_mode', %s)
                           ON DUPLICATE KEY UPDATE
                               setting_value=VALUES(setting_value)""", (mode,))
        cur.execute("UPDATE countries SET enabled=1 WHERE iso='IN'")
        cur.execute("UPDATE voice_providers SET enabled=1")
        cur.execute("UPDATE call_routes SET enabled=1 "
                    "WHERE route_key IN ('IN_PROVEEDOR1','IN_STRINGEE')")
    c.close()
    return name


class DB:
    """Mismo interfaz que panel/app/analytics.DB, para reusar los módulos de
    referencia (call_jobs, ops_events, tool_requests…) en los tests."""

    def __init__(self, conn, driver='mysql'):
        self.conn, self.driver = conn, driver
        self.ph = '%s' if driver == 'mysql' else '?'

    def q(self, sql, params=()):
        sql = sql.replace('§', self.ph)
        cur = self.conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        return out

    def one(self, sql, params=()):
        r = self.q(sql, params)
        return r[0] if r else {}

    def table_exists(self, name):
        try:
            self.q(f"SELECT 1 FROM {name} LIMIT 1")
            return True
        except Exception:
            return False

    def execute(self, sql, params=()):
        sql = sql.replace('§', self.ph)
        cur = self.conn.cursor()
        cur.execute(sql, params)
        lastrowid, rowcount = cur.lastrowid, cur.rowcount
        cur.close()
        self.conn.commit()
        return lastrowid, rowcount


def db_for(name):
    return DB(mysql_conn(name), 'mysql')


# ── workflows ─────────────────────────────────────────────────────────
def load_workflows():
    import json
    out = {}
    for f in sorted(os.listdir(WORKFLOWS)):
        if f.endswith('.json') and f != 'BUILD_REPORT.json':
            out[f] = json.load(open(os.path.join(WORKFLOWS, f), encoding='utf-8'))
    return out


def load_js():
    return {f: open(os.path.join(JS_DIR, f), encoding='utf-8').read()
            for f in sorted(os.listdir(JS_DIR)) if f.endswith('.js')}


def node_types(wf, t):
    return [n for n in wf['nodes'] if n['type'] == t]


def code_nodes(wf):
    return node_types(wf, 'n8n-nodes-base.code')


def mysql_nodes(wf):
    return node_types(wf, 'n8n-nodes-base.mySql')


def run_node(js_source, script, payload=None):
    """Ejecuta un fragmento JS bajo node con las librerías del suite cargadas."""
    import json
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
        fh.write(js_source + '\n')
        fh.write('const __payload = ' + json.dumps(payload or {}) + ';\n')
        fh.write(script)
        path = fh.name
    r = subprocess.run(['node', path], capture_output=True, text=True, timeout=120)
    os.unlink(path)
    if r.returncode != 0:
        raise AssertionError(f'node fallo: {r.stderr[:600]}')
    return json.loads(r.stdout.strip().splitlines()[-1])
