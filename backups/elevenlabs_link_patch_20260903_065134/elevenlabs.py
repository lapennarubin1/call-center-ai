"""Configuracion de ElevenLabs por tenant — SIN conectar nada real.

Este modulo NO llama a la API de ElevenLabs, NO gasta creditos, y NO
guarda ningun valor de API key en la base de datos.

Dos modos, ambos preparados pero sin conexion real todavia:

  managed: en el futuro la plataforma usara su propia cuenta de ElevenLabs
           para este tenant. No requiere ningun secreto del tenant ahora.
  byok:    el tenant va a usar su propia API key ("Bring Your Own Key").

Nota de seguridad — por que no se guarda la key en este bloque:
tenant_integrations.config_ref esta diseñado para guardar el NOMBRE de un
secreto (variable de entorno), nunca su valor (Documento 11, seccion 5.3;
mismo patron que ya usan sip/telegram/okpay). El proyecto todavia no tiene
infraestructura de cifrado en reposo (vault/KMS) para guardar el VALOR de
una API key de tercero de forma segura en la base de datos. Escribir una
funcion propia de encrypt/decrypt con una clave fija en el codigo (cripto
casera) seria peor que no cifrar: da una falsa sensacion de seguridad y
complica migrar despues a un vault real.

Por eso, en modo BYOK, la plataforma solo registra: (1) que el tenant
eligio BYOK, y (2) el NOMBRE de variable de entorno convencional donde un
operador humano debe cargar la key manualmente en el .env del servidor —
igual que ya se hace para el resto de integraciones del proyecto. Este
modulo solo puede verificar si esa variable esta PRESENTE o no (booleano),
nunca leer ni exponer su valor.
"""
from ...core.integrations.secrets import resolve_secret

PROVIDER_KEY = "elevenlabs"
VALID_MODES = {"managed", "byok"}


def config_ref_for(tenant_id: int, mode: str) -> str:
    """Nombre de variable de entorno asociada a este tenant+modo (convencion
    TENANT_<id>_<PROVIDER>_KEY ya usada en Documento 14, seccion 3.6).
    'managed' no depende de un secreto del tenant; se usa un valor
    autodescriptivo (config_ref es NOT NULL en el esquema, no puede ser
    vacio), nunca el nombre de una variable real."""
    if mode == "byok":
        return f"TENANT_{tenant_id}_ELEVENLABS_KEY"
    return "PLATFORM_MANAGED"


def is_configured(mode: str, config_ref: str) -> bool:
    """Solo indica presencia — jamas devuelve ni expone el valor del secreto."""
    if mode == "managed":
        return True  # gestionado por la plataforma; no depende de un secreto del tenant todavia
    return resolve_secret(config_ref) is not None
