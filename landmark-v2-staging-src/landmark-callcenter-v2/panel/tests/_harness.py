"""Arnés mínimo compartido por las suites. Sin dependencias externas."""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
APP = os.path.join(ROOT, 'app')
MIGRATION = os.path.join(ROOT, 'migrations', 'MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_2.sql')
V21_MIGRATION = os.path.join(ROOT, 'migrations', 'legacy', 'MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_1.sql')
sys.path.insert(0, APP)
os.environ.setdefault('LM_SCHEDULER_DISABLED', '1')
os.environ.setdefault('LM_TELEGRAM_DISABLED', '1')
# ── añadido por el suite V2 (r2-final2): higiene del paquete ──────────
# Correr los tests no puede dejar basura dentro de lo que se entrega.
#   · sin LM_SECRET, `import server` genera una clave de sesión y la
#     ESCRIBE en panel/app/.session_key — un secreto dentro del paquete
#   · sin esto, Python deja .pyc y __pycache__ en panel/app/
# Lo comprueba tests/test_package_hygiene_v2.py.
# SECSCAN-OK: clave de sesión de test, no es un secreto real
os.environ.setdefault('LM_SECRET', 'test-only-session-key-not-a-real-secret')
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
sys.dont_write_bytecode = True

MYSQL = {
    'host': os.getenv('LM_TEST_MYSQL_HOST', '127.0.0.1'),
    'user': os.getenv('LM_TEST_MYSQL_USER', 'lmtest'),
    # Credencial de un MariaDB local y desechable que sólo existe
    # mientras corren los tests. No da acceso a nada real.
    # SECSCAN-OK: credencial de test, MariaDB local desechable
    'password': os.getenv('LM_TEST_MYSQL_PASS', 'lmtest'),
    'port': int(os.getenv('LM_TEST_MYSQL_PORT', '3306')),
    'unix_socket': os.getenv('LM_TEST_MYSQL_SOCKET', '/run/mysqld/mysqld.sock'),
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
            traceback.print_exc(limit=3)

    def skip(self, name, why):
        self.skipped.append((name, why))
        print(f"  skip  {name} ({why})")

    def finish(self):
        print('\n' + '═' * 70)
        print(f"  {self.name}: {len(self.passed)} PASS · {len(self.failed)} FAIL · {len(self.skipped)} SKIP")
        for n, e in self.failed:
            print(f"   ✗ {n}: {e}")
        print('═' * 70)
        return 1 if self.failed else 0


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
    """Separa el .sql en sentencias respetando literales entre comillas simples
    (con '' y \\' como escapes). Quita comentarios -- de línea completa y
    en línea (fuera de literales)."""
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
    """Ejecuta la migración sentencia por sentencia en UNA sesión.
    limit=N corta tras N sentencias (simula una ejecución interrumpida)."""
    stmts = split_sql(open(path).read())
    c = mysql_conn(dbname)
    with c.cursor() as cur:
        for i, s in enumerate(stmts):
            if limit is not None and i >= limit:
                break
            cur.execute(s)
    c.close()
    return len(stmts)
