"""
JARVIS — Configuración central
===============================
Todas las variables de entorno en UN solo lugar. Ningún otro módulo
lee os.getenv() directamente: así, cuando haya que cambiar una
credencial o agregar un servicio, se toca este archivo y nada más.

Las credenciales NUNCA van hardcodeadas — se leen del .env que vive
fuera del repo (por eso este proyecto se puede publicar open source
sin filtrar nada).
"""
import os


def _env(key, default=''):
    return os.getenv(key, default).strip()


def _env_int(key, default):
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


class Config:
    # ── Identidad del asistente ─────────────────────────────────
    ASSISTANT_NAME = _env('JARVIS_NAME', 'JARVIS')
    OPERATOR_NAME  = _env('JARVIS_OPERATOR', '')   # cómo te llama a vos

    # ── Servidor ────────────────────────────────────────────────
    PORT       = _env_int('JARVIS_PORT', 8090)
    SECRET_KEY = _env('JARVIS_SECRET_KEY', '')
    USER       = _env('JARVIS_USER', 'admin')
    PASS       = _env('JARVIS_PASS', '')

    # ── Cerebro (Anthropic) ─────────────────────────────────────
    ANTHROPIC_API_KEY = _env('ANTHROPIC_API_KEY')
    ANTHROPIC_MODEL   = _env('JARVIS_MODEL', 'claude-sonnet-4-6')
    MAX_TOKENS        = _env_int('JARVIS_MAX_TOKENS', 4096)
    # Cuántas vueltas de "pensar -> usar herramienta -> pensar" puede
    # dar antes de cortar. Evita loops infinitos si una skill falla
    # siempre y el modelo insiste en reintentarla.
    MAX_TOOL_ROUNDS   = _env_int('JARVIS_MAX_TOOL_ROUNDS', 8)

    # ── Voz ─────────────────────────────────────────────────────
    ELEVENLABS_API_KEY = _env('ELEVENLABS_API_KEY')
    # Voces por idioma. Si una está vacía, se cae a VOICE_DEFAULT.
    # Nunca se mezclan: el idioma detectado elige UNA voz y esa se usa
    # para toda la respuesta.
    VOICE_DEFAULT = _env('JARVIS_VOICE_DEFAULT', 'JBFqnCBsd6RMkjVDRZzb')
    VOICE_ES      = _env('JARVIS_VOICE_ES', '')
    VOICE_EN      = _env('JARVIS_VOICE_EN', '')
    VOICE_HI      = _env('JARVIS_VOICE_HI', '')
    TTS_MODEL     = _env('JARVIS_TTS_MODEL', 'eleven_turbo_v2_5')
    STT_MODEL     = _env('JARVIS_STT_MODEL', 'scribe_v1')
    VOICE_ENABLED = _env('JARVIS_VOICE_ENABLED', '1') != '0'

    # ── Base de datos del panel (solo lectura para métricas) ────
    DB_HOST = _env('LM_DB_HOST', 'localhost')
    DB_USER = _env('LM_DB_USER', 'panel_rw')
    DB_PASS = _env('LM_DB_PASS')
    DB_NAME = _env('LM_DB_NAME', 'asterisk')
    DB_PORT = _env_int('LM_DB_PORT', 3306)
    SQLITE  = _env('JARVIS_SQLITE')   # modo test/demo

    # Base propia de Jarvis (memoria, historial, aprendizaje).
    # Separada de la del panel a propósito: Jarvis escribe mucho y no
    # queremos que su historial ensucie la base operativa.
    JARVIS_DB = _env('JARVIS_DB_PATH', '/opt/jarvis/data/jarvis.db')

    # ── Integraciones ───────────────────────────────────────────
    N8N_BASE_URL = _env('LM_N8N_BASE_URL', '')
    N8N_API_KEY  = _env('LM_N8N_API_KEY')
    PANEL_URL    = _env('JARVIS_PANEL_URL', 'http://localhost:8080')

    ASTERISK_ENABLED = _env('JARVIS_ASTERISK', '1') != '0'

    # ── Seguridad de acciones ───────────────────────────────────
    # Skills que MODIFICAN algo (apagar el call center, cambiar un
    # horario) requieren confirmación explícita del operador antes de
    # ejecutarse. Se puede desactivar, pero por defecto está en on:
    # que una frase mal entendida por el micrófono apague la
    # operación de un país entero es un riesgo real.
    CONFIRM_WRITES = _env('JARVIS_CONFIRM_WRITES', '1') != '0'

    @classmethod
    def voice_for_language(cls, lang):
        """Una voz por idioma, nunca mezcladas dentro de una respuesta."""
        return {
            'es': cls.VOICE_ES,
            'en': cls.VOICE_EN,
            'hi': cls.VOICE_HI,
        }.get(lang) or cls.VOICE_DEFAULT

    @classmethod
    def missing_required(cls):
        """
        Qué falta para que Jarvis arranque de verdad. Se muestra en
        /health y en el arranque — mejor un error claro al inicio que
        un fallo raro a mitad de una conversación.
        """
        missing = []
        if not cls.ANTHROPIC_API_KEY:
            missing.append('ANTHROPIC_API_KEY (cerebro)')
        if not cls.PASS:
            missing.append('JARVIS_PASS (login)')
        if not cls.SQLITE and not cls.DB_PASS:
            missing.append('LM_DB_PASS (métricas del panel)')
        if cls.VOICE_ENABLED and not cls.ELEVENLABS_API_KEY:
            missing.append('ELEVENLABS_API_KEY (voz; o poné JARVIS_VOICE_ENABLED=0)')
        return missing


CFG = Config
