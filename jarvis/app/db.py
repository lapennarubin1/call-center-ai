"""
JARVIS — Capa de base de datos
===============================
Dos bases distintas, a propósito:

  1. La del PANEL (MySQL `asterisk`) — de donde salen las métricas
     reales: CDR, leads, conversiones, SIP. Jarvis la lee y, cuando
     una skill de escritura lo pide, escribe en las tablas del panel
     (mismos permisos que el panel).

  2. La PROPIA de Jarvis (SQLite) — historial de conversaciones,
     memoria de largo plazo y las lecciones que va aprendiendo. Está
     separada porque Jarvis escribe mucho y seguido; mezclarlo con la
     base operativa del call center sería pedir problemas.

El placeholder '§' se reemplaza por el de cada driver ('%s' en MySQL,
'?' en SQLite) — así una misma query sirve en producción (MySQL) y en
los tests (SQLite) sin reescribirla.
"""
import sqlite3
import os
import threading
from .config import CFG


class DB:
    def __init__(self, conn, driver):
        self.conn = conn
        self.driver = driver
        self.ph = '%s' if driver == 'mysql' else '?'

    def q(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql.replace('§', self.ph), params)
        cols = [d[0] for d in cur.description] if cur.description else []
        out = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur.close()
        return out

    def one(self, sql, params=()):
        rows = self.q(sql, params)
        return rows[0] if rows else {}

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql.replace('§', self.ph), params)
        lastrowid, rowcount = cur.lastrowid, cur.rowcount
        cur.close()
        self.conn.commit()
        return lastrowid, rowcount

    def insert(self, sql, params=()):
        return self.execute(sql, params)[0]

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def panel_db():
    """Conexión a la base del panel (métricas del call center)."""
    if CFG.SQLITE:
        conn = sqlite3.connect(CFG.SQLITE)
        _register_sqlite_regexp(conn)
        return DB(conn, 'sqlite')
    import pymysql
    conn = pymysql.connect(
        host=CFG.DB_HOST, user=CFG.DB_USER, password=CFG.DB_PASS,
        database=CFG.DB_NAME, port=CFG.DB_PORT, charset='utf8mb4',
        autocommit=True, connect_timeout=10, read_timeout=60)
    return DB(conn, 'mysql')


def _register_sqlite_regexp(conn):
    """
    SQLite no trae REGEXP nativo, pero las queries del panel lo usan
    para filtrar destinos por país (dst REGEXP '^[+]?91...'). Sin esto
    los tests en SQLite fallarían con una query que en MySQL anda bien.
    """
    import re
    conn.create_function(
        'REGEXP', 2,
        lambda pat, val: bool(re.search(pat, val)) if val is not None else False)


_jarvis_lock = threading.Lock()


def jarvis_db():
    """Base propia de Jarvis (memoria e historial)."""
    path = CFG.JARVIS_DB
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.execute('PRAGMA journal_mode=WAL')   # varios workers a la vez
    return DB(conn, 'sqlite')


def init_jarvis_schema():
    """Idempotente — se puede llamar en cada arranque sin miedo."""
    with _jarvis_lock:
        db = jarvis_db()
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                language TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_conv_session
                          ON conversations(session_id, id)""")

            # Memoria de largo plazo: hechos que el operador le dijo a
            # Jarvis y quiere que recuerde entre sesiones.
            db.execute("""CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_topic
                          ON memories(topic)""")

            # Aprendizaje: correcciones del operador ("no, cuando digo
            # X me refiero a Y"). Se inyectan en el prompt de las
            # siguientes conversaciones.
            db.execute("""CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson TEXT NOT NULL,
                context TEXT,
                times_applied INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")

            # Telemetría de skills: qué se usa, qué falla, cuánto tarda.
            # Sirve para dos cosas: diagnosticar, y que Jarvis sepa que
            # una skill viene fallando y avise en vez de insistir.
            db.execute("""CREATE TABLE IF NOT EXISTS skill_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill TEXT NOT NULL,
                ok INTEGER NOT NULL,
                ms INTEGER,
                error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_skillruns_skill
                          ON skill_runs(skill, created_at)""")

            # Acciones de escritura ejecutadas — auditoría. Si mañana
            # el call center amaneció apagado, acá está quién y cuándo.
            db.execute("""CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                skill TEXT NOT NULL,
                params TEXT,
                result TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
        finally:
            db.close()
