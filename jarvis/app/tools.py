"""
JARVIS — Herramientas de línea de comandos
===========================================
Diagnóstico sin levantar el servidor. Sirve para verificar que todo
esté bien conectado ANTES de arrancar, que es cuando conviene
enterarse de que falta una clave.

    python3 -m app.tools check     verifica configuración y conexiones
    python3 -m app.tools voices    lista las voces de ElevenLabs
    python3 -m app.tools skills    catálogo de capacidades
    python3 -m app.tools try <skill> [k=v ...]   ejecuta una skill
"""
import sys
import json
import types

from .config import CFG


def _ctx():
    from .db import panel_db, jarvis_db, init_jarvis_schema
    init_jarvis_schema()
    return types.SimpleNamespace(panel_db=panel_db(), jdb=jarvis_db(),
                                 session_id='cli', charts=[], language='es')


def cmd_check():
    print(f'\n  {CFG.ASSISTANT_NAME} — diagnóstico\n')
    problemas = []

    faltantes = CFG.missing_required()
    if faltantes:
        print('  Configuración incompleta:')
        for f in faltantes:
            print(f'    ✗ {f}')
        problemas.append('configuración')
    else:
        print('    ✓ configuración completa')

    # Base del panel
    try:
        from .db import panel_db
        db = panel_db()
        db.one('SELECT 1 AS x')
        tablas = []
        for t in ('cdr', 'panel_conversions', 'panel_leads',
                  'n8n_switches', 'sip_providers'):
            try:
                db.one(f'SELECT 1 FROM {t} LIMIT 1')
                tablas.append(t)
            except Exception:
                pass
        db.close()
        print(f'    ✓ base del panel ({len(tablas)} tablas accesibles: {", ".join(tablas)})')
        for esperada in ('n8n_switches', 'sip_providers'):
            if esperada not in tablas:
                print(f'      ! falta {esperada} — algunas skills no van a funcionar')
    except Exception as ex:
        print(f'    ✗ base del panel: {ex}')
        problemas.append('base de datos')

    # Base propia
    try:
        from .db import jarvis_db, init_jarvis_schema
        init_jarvis_schema()
        db = jarvis_db()
        n = db.one('SELECT COUNT(*) AS n FROM memories')['n']
        db.close()
        print(f'    ✓ memoria de Jarvis ({n} recuerdos)')
    except Exception as ex:
        print(f'    ✗ memoria de Jarvis: {ex}')
        problemas.append('memoria')

    # n8n
    if CFG.N8N_API_KEY:
        try:
            from .integrations import n8n_list_workflows
            wfs = n8n_list_workflows()
            activos = sum(1 for w in wfs if w['active'])
            print(f'    ✓ n8n ({len(wfs)} workflows, {activos} activos)')
        except Exception as ex:
            print(f'    ✗ n8n: {ex}')
            problemas.append('n8n')
    else:
        print('    - n8n sin configurar (el control del call center no va a andar)')

    # Asterisk
    if CFG.ASTERISK_ENABLED:
        try:
            from .integrations import asterisk_cli, parse_pjsip_endpoints
            eps = parse_pjsip_endpoints(asterisk_cli('pjsip show endpoints'))
            print(f'    ✓ Asterisk ({len(eps)} endpoints)')
        except Exception as ex:
            print(f'    - Asterisk: {ex}')

    # Voz
    if CFG.VOICE_ENABLED and CFG.ELEVENLABS_API_KEY:
        try:
            from .voice import available_voices
            voces = available_voices()
            print(f'    ✓ ElevenLabs ({len(voces)} voces disponibles)')
        except Exception as ex:
            print(f'    ✗ ElevenLabs: {ex}')
            problemas.append('voz')

    from . import skills as sk
    print(f'    ✓ {len(sk.all_skills())} skills cargadas')

    print()
    if problemas:
        print(f'  Hay que resolver: {", ".join(problemas)}\n')
        return 1
    print('  Todo en orden.\n')
    return 0


def cmd_voices():
    from .voice import available_voices, VoiceError
    try:
        voces = available_voices()
    except VoiceError as ex:
        print(f'  Error: {ex}')
        return 1
    print(f'\n  {len(voces)} voces en la cuenta:\n')
    for v in voces:
        idiomas = f" · {v['idiomas']}" if v['idiomas'] else ''
        print(f"    {v['id']}   {v['nombre']}{idiomas}")
    print('\n  Copiá el ID que quieras a JARVIS_VOICE_ES / _EN / _HI en el .env\n')
    return 0


def cmd_skills():
    from . import skills as sk
    todas = sk.all_skills()
    print(f'\n  {len(todas)} capacidades:\n')
    for nombre, spec in sorted(todas.items(), key=lambda x: (x[1]['category'], x[0])):
        marca = '✎' if spec['category'] == 'write' else '·'
        desc = spec['description'].split(' Ejemplos')[0]
        print(f'    {marca} {nombre}')
        print(f'        {desc[:96]}')
        if spec['examples']:
            print(f'        ej: "{spec["examples"][0]}"')
    print('\n    ✎ = modifica el sistema (pide confirmación)\n')
    return 0


def cmd_try(argv):
    if not argv:
        print('  Uso: python3 -m app.tools try <skill> [clave=valor ...]')
        return 1
    from . import skills as sk
    nombre = argv[0]
    if not sk.get_skill(nombre):
        print(f'  No existe la skill "{nombre}". Vela con: python3 -m app.tools skills')
        return 1

    params = {}
    for arg in argv[1:]:
        if '=' not in arg:
            print(f'  Parámetro inválido: {arg!r} (formato clave=valor)')
            return 1
        k, v = arg.split('=', 1)
        # Tipar lo obvio: "dias=7" tiene que llegar como int, no "7"
        if v.isdigit():
            v = int(v)
        elif v.startswith('[') or v.startswith('{'):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
        params[k] = v

    ctx = _ctx()
    try:
        resultado = sk.run_skill(nombre, params, ctx)
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
        return 1 if 'error' in resultado else 0
    finally:
        ctx.panel_db.close()
        ctx.jdb.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    comando = sys.argv[1]
    if comando == 'check':
        return cmd_check()
    if comando == 'voices':
        return cmd_voices()
    if comando == 'skills':
        return cmd_skills()
    if comando == 'try':
        return cmd_try(sys.argv[2:])
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main())
