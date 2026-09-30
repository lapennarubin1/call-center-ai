"""
JARVIS — Registro de skills
============================
Importar este paquete registra TODAS las skills disponibles. Cada
módulo se importa acá una sola vez; el decorador @skill hace el resto.

Para agregar un módulo de skills nuevo: crearlo al lado y sumarlo a
_MODULOS. Nada más — ni el cerebro ni el servidor se tocan.
"""
from . import base                      # noqa: F401  (define el registro)

_MODULOS = ['metrics', 'control', 'sip_and_memory']

for _nombre in _MODULOS:
    __import__(f'{__name__}.{_nombre}', fromlist=['*'])

from .base import (all_skills, get_skill, run_skill, anthropic_tools,   # noqa: E402
                   failing_skills, SkillError, READ, WRITE)

__all__ = ['all_skills', 'get_skill', 'run_skill', 'anthropic_tools',
           'failing_skills', 'SkillError', 'READ', 'WRITE']
