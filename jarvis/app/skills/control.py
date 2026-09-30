"""
JARVIS — Skills de control del call center
===========================================
Acá viven las capacidades que MODIFICAN el sistema real: prender y
apagar los workflows de n8n, cambiar horarios automáticos.

Todas las de escritura están marcadas category=WRITE y llevan un
`confirm_prompt`: Jarvis repite en voz alta qué va a hacer y espera un
"sí" antes de ejecutar. Es a propósito. El micrófono se equivoca, el
ruido de fondo se equivoca, y apagar la operación de un país por una
palabra mal entendida es un error caro y silencioso — nadie se entera
hasta que a la mañana siguiente no salió ninguna llamada.

Las de lectura (ver estado) corren directo, sin confirmar.
"""
from .base import skill, SkillError, READ, WRITE
from ..integrations import (n8n_list_workflows, n8n_set_workflow,
                            asterisk_cli, parse_pjsip_endpoints,
                            parse_pjsip_registrations, IntegrationError)


def _switches(ctx):
    """Los switches del Call Center viven en la base del panel (v24+)."""
    db = ctx.panel_db
    try:
        rows = db.q("SELECT id, label, workflow_ids FROM n8n_switches ORDER BY id")
    except Exception:
        raise SkillError(
            'no encuentro la tabla de switches del Call Center. '
            '¿Está desplegada la versión v24 o superior del panel?')
    for r in rows:
        r['workflow_ids'] = [w for w in (r['workflow_ids'] or '').split(',') if w]
    return rows


def _find_switch(ctx, nombre):
    """
    Busca un switch por nombre de forma tolerante: "india" encuentra
    "INDIA", "Panel India" o "india call center". La gente no dice el
    label exacto cuando habla.
    """
    needle = (nombre or '').strip().lower()
    if not needle:
        raise SkillError('falta indicar qué switch')
    switches = _switches(ctx)
    exactos = [s for s in switches if s['label'].lower() == needle]
    if exactos:
        return exactos[0]
    parciales = [s for s in switches if needle in s['label'].lower()]
    if len(parciales) == 1:
        return parciales[0]
    if len(parciales) > 1:
        opciones = ', '.join(s['label'] for s in parciales)
        raise SkillError(
            f'"{nombre}" coincide con varios switches ({opciones}). Necesito el nombre exacto.')
    disponibles = ', '.join(s['label'] for s in switches) or 'ninguno configurado'
    raise SkillError(f'no encontré un switch llamado "{nombre}". Disponibles: {disponibles}')


@skill(
    name='estado_call_center',
    description=(
        'Estado actual de todos los switches del call center: qué está prendido, '
        'qué apagado, cuántos workflows tiene cada uno y su horario automático.'),
    params={},
    required=[],
    category=READ,
    examples=['cómo está el call center', 'qué está prendido ahora', 'está corriendo India'],
)
def estado_call_center(ctx):
    switches = _switches(ctx)
    try:
        workflows = {w['id']: w for w in n8n_list_workflows()}
        n8n_ok, n8n_error = True, None
    except IntegrationError as ex:
        workflows, n8n_ok, n8n_error = {}, False, str(ex)

    # Los horarios están en el panel (v26+); si la tabla no existe
    # todavía, no es un error — simplemente no hay horarios.
    horarios = {}
    try:
        for h in ctx.panel_db.q("SELECT * FROM n8n_switch_schedules"):
            horarios[h['switch_id']] = h
    except Exception:
        pass

    salida = []
    for sw in switches:
        detalles, activos = [], 0
        for wid in sw['workflow_ids']:
            w = workflows.get(wid)
            if w:
                detalles.append({'nombre': w['name'], 'activo': w['active']})
                activos += 1 if w['active'] else 0
            else:
                detalles.append({'nombre': f'(id {wid} no encontrado en n8n)', 'activo': None})

        item = {
            'switch': sw['label'],
            'workflows_totales': len(sw['workflow_ids']),
            'workflows_activos': activos if n8n_ok else None,
            'estado': ('todo prendido' if n8n_ok and activos == len(sw['workflow_ids']) and activos
                       else 'todo apagado' if n8n_ok and activos == 0
                       else f'{activos} de {len(sw["workflow_ids"])} prendidos' if n8n_ok
                       else 'desconocido (n8n no responde)'),
            'workflows': detalles,
        }
        h = horarios.get(sw['id'])
        if h:
            item['horario'] = {
                'prende': h['on_time'], 'apaga': h['off_time'],
                'zona_horaria': h['timezone'],
                'dias': [d for d in (h['days'] or '').split(',') if d],
                'activo': bool(h['enabled']),
                'ultimo_error': h.get('last_error'),
            }
        salida.append(item)

    resultado = {'switches': salida}
    if not n8n_ok:
        resultado['advertencia'] = (
            f'No pude leer el estado real desde n8n: {n8n_error}. '
            f'Los switches existen pero no sé si están prendidos.')
    return resultado


@skill(
    name='prender_call_center',
    description=(
        'PRENDE todos los workflows de un switch del call center (por ejemplo "India" '
        'o "México"). Esto reactiva las llamadas automáticas de ese país.'),
    params={'switch': {'type': 'string', 'description': 'Nombre del switch, ej: India, México, Panel'}},
    required=['switch'],
    category=WRITE,
    confirm_prompt='Voy a PRENDER el switch "{switch}" — se reactivan las llamadas automáticas. ¿Confirmás?',
    examples=['prendé India', 'activá el call center de México', 'encendé todo'],
)
def prender_call_center(switch, ctx):
    return _set_switch(ctx, switch, True)


@skill(
    name='apagar_call_center',
    description=(
        'APAGA todos los workflows de un switch del call center. Las llamadas ya '
        'en curso terminan normalmente; deja de iniciar nuevas.'),
    params={'switch': {'type': 'string', 'description': 'Nombre del switch, ej: India, México, Panel'}},
    required=['switch'],
    category=WRITE,
    confirm_prompt='Voy a APAGAR el switch "{switch}" — se detienen las llamadas automáticas nuevas. ¿Confirmás?',
    examples=['apagá México', 'frená el call center de India', 'pará todo'],
)
def apagar_call_center(switch, ctx):
    return _set_switch(ctx, switch, False)


def _set_switch(ctx, nombre, prender):
    sw = _find_switch(ctx, nombre)
    if not sw['workflow_ids']:
        raise SkillError(f'el switch "{sw["label"]}" no tiene workflows configurados')

    # Best-effort: si uno falla, los demás igual se aplican. Un
    # workflow roto no puede dejar el resto del país a medio apagar.
    ok, errores = [], []
    for wid in sw['workflow_ids']:
        try:
            n8n_set_workflow(wid, prender)
            ok.append(wid)
        except IntegrationError as ex:
            errores.append(f'{wid}: {ex}')

    resultado = {
        'switch': sw['label'],
        'accion': 'prendido' if prender else 'apagado',
        'workflows_afectados': len(ok),
        'workflows_totales': len(sw['workflow_ids']),
    }
    if errores:
        resultado['errores'] = errores
        resultado['advertencia'] = (
            f'{len(errores)} de {len(sw["workflow_ids"])} workflows fallaron. '
            f'El switch quedó a medias — conviene revisarlo.')
    return resultado


@skill(
    name='cambiar_horario',
    description=(
        'Cambia el horario automático de encendido/apagado de un switch. Las horas '
        'van en formato 24h (HH:MM) y en la zona horaria que se indique.'),
    params={
        'switch': {'type': 'string', 'description': 'Nombre del switch'},
        'hora_prende': {'type': 'string', 'description': 'Hora de encendido, formato HH:MM (24h)'},
        'hora_apaga': {'type': 'string', 'description': 'Hora de apagado, formato HH:MM (24h)'},
        'zona_horaria': {'type': 'string', 'description': 'Zona IANA, ej: Asia/Dubai, America/Mexico_City, Asia/Kolkata'},
        'dias': {'type': 'array', 'items': {'type': 'string'},
                 'description': 'Días: mon, tue, wed, thu, fri, sat, sun'},
    },
    required=['switch', 'hora_prende', 'hora_apaga', 'zona_horaria'],
    category=WRITE,
    confirm_prompt=('Voy a cambiar el horario de "{switch}": prende {hora_prende}, '
                    'apaga {hora_apaga} ({zona_horaria}). ¿Confirmás?'),
    examples=['cambiá el horario de India a las 9 de la mañana',
              'que México apague a las 8 de la noche'],
)
def cambiar_horario(switch, hora_prende, hora_apaga, zona_horaria, ctx, dias=None):
    import re
    from zoneinfo import available_timezones

    sw = _find_switch(ctx, switch)
    hhmm = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
    for valor, etiqueta in [(hora_prende, 'hora de encendido'), (hora_apaga, 'hora de apagado')]:
        if not hhmm.match((valor or '').strip()):
            raise SkillError(f'{etiqueta} inválida: {valor!r}. Tiene que ser HH:MM en 24 horas.')
    if zona_horaria not in available_timezones():
        raise SkillError(f'zona horaria desconocida: {zona_horaria!r}')

    validos = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    lista = [d.strip().lower()[:3] for d in (dias or ['mon', 'tue', 'wed', 'thu', 'fri']) if d]
    invalidos = [d for d in lista if d not in validos]
    if invalidos:
        raise SkillError(f'días inválidos: {invalidos}. Usá: {", ".join(validos)}')
    lista = [d for d in validos if d in lista]
    if not lista:
        raise SkillError('hay que elegir al menos un día')

    db = ctx.panel_db
    try:
        existe = db.one("SELECT id FROM n8n_switch_schedules WHERE switch_id = §", (sw['id'],))
        if existe:
            db.execute("""UPDATE n8n_switch_schedules
                          SET timezone=§, on_time=§, off_time=§, days=§, enabled=1
                          WHERE switch_id=§""",
                       (zona_horaria, hora_prende, hora_apaga, ','.join(lista), sw['id']))
        else:
            db.execute("""INSERT INTO n8n_switch_schedules
                          (switch_id, timezone, on_time, off_time, days, enabled)
                          VALUES (§,§,§,§,§,1)""",
                       (sw['id'], zona_horaria, hora_prende, hora_apaga, ','.join(lista)))
    except SkillError:
        raise
    except Exception as ex:
        raise SkillError(
            f'no pude guardar el horario ({ex}). ¿Está desplegada la v26 del panel?')

    return {
        'switch': sw['label'],
        'prende': hora_prende,
        'apaga': hora_apaga,
        'zona_horaria': zona_horaria,
        'dias': lista,
        'nota': 'El cambio ya está activo; el scheduler lo toma en el próximo chequeo (menos de un minuto).',
    }


@skill(
    name='estado_pbx',
    description=(
        'Estado en vivo del PBX Asterisk: trunks SIP registrados, endpoints '
        'disponibles y si el proveedor está respondiendo.'),
    params={},
    required=[],
    category=READ,
    examples=['está bien el PBX', 'los trunks están registrados', 'estado de Asterisk'],
)
def estado_pbx(ctx):
    try:
        endpoints = parse_pjsip_endpoints(asterisk_cli('pjsip show endpoints'))
        registros = parse_pjsip_registrations(asterisk_cli('pjsip show registrations'))
    except IntegrationError as ex:
        raise SkillError(str(ex))

    caidos = [e for e in endpoints if 'unavail' in e['estado'].lower()]
    return {
        'endpoints': endpoints,
        'registros': registros,
        'endpoints_totales': len(endpoints),
        'endpoints_no_disponibles': len(caidos),
        'alerta': (f'{len(caidos)} endpoint(s) no disponibles: '
                   + ', '.join(e['nombre'] for e in caidos)) if caidos else None,
    }
