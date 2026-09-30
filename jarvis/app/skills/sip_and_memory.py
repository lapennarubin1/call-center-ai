"""
JARVIS — Skills de saldo SIP, gráficas y memoria
=================================================
Tres grupos:

  SIP     — saldo del proveedor: depositado vs consumido, y cuánto
            queda. Es la métrica que decide si mañana hay llamadas.

  GRÁFICA — no dibuja: devuelve una ESPECIFICACIÓN de gráfico que el
            frontend renderiza. Así la misma respuesta sirve para la
            pantalla y para la voz, y el modelo elige qué mostrar sin
            generar HTML.

  MEMORIA — lo que el operador le pide recordar entre sesiones, y las
            correcciones que le hace ("cuando digo X me refiero a Y").
            Esto es lo que hace que Jarvis mejore con el uso en vez de
            arrancar de cero cada vez.
"""
import json
from .base import skill, SkillError, READ, WRITE
from .metrics import COUNTRIES, resolve_country, resolve_period


# ══════════════════════════════════════════════════════════════════
#  SALDO SIP
# ══════════════════════════════════════════════════════════════════

@skill(
    name='saldo_sip',
    description=(
        'Saldo del proveedor SIP: cuánto se depositó, cuánto se consumió en '
        'llamadas y cuánto queda disponible. Incluye el desglose por país.'),
    params={},
    required=[],
    category=READ,
    examples=['cuánto saldo nos queda', 'cómo está el balance del proveedor',
              'nos alcanza el crédito SIP'],
)
def saldo_sip(ctx):
    db = ctx.panel_db
    try:
        proveedores = db.q(
            "SELECT id, name, billing_start_date FROM sip_providers WHERE active = 1")
    except Exception:
        raise SkillError(
            'no encuentro las tablas de SIP Balance. ¿Está desplegada la v21+ del panel?')
    if not proveedores:
        raise SkillError('no hay proveedores SIP configurados en el panel')

    salida = []
    for prov in proveedores:
        corte = prov.get('billing_start_date')

        params = [prov['id']]
        sql_dep = "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM sip_deposits WHERE provider_id = §"
        if corte:
            sql_dep += " AND deposit_date >= §"
            params.append(str(corte))
        depositado = float(db.one(sql_dep, tuple(params)).get('total') or 0)

        precios = db.q("""SELECT country, price_per_minute FROM sip_provider_pricing
                          WHERE provider_id = §""", (prov['id'],))
        consumo_total, desglose = 0.0, []
        for row in precios:
            pais = (row['country'] or '').lower()
            cfg = COUNTRIES.get(pais)
            if not cfg:
                continue
            precio = float(row['price_per_minute'] or 0)
            sql = f"""SELECT COALESCE(SUM(CAST((billsec + 59) / 60 AS INTEGER)), 0) AS mins
                      FROM cdr WHERE {cfg['dst_regex']} AND disposition = 'ANSWERED'"""
            p = []
            if corte:
                sql += " AND calldate >= §"
                p.append(str(corte))
            minutos = int(db.one(sql, tuple(p)).get('mins') or 0)
            costo = round(minutos * precio, 2)
            consumo_total += costo
            if minutos:
                desglose.append({
                    'pais': cfg['label'], 'minutos': minutos,
                    'precio_por_minuto': precio, 'costo_usd': costo,
                })

        restante = round(depositado - consumo_total, 2)
        item = {
            'proveedor': prov['name'],
            'depositado_usd': round(depositado, 2),
            'consumido_usd': round(consumo_total, 2),
            'saldo_restante_usd': restante,
            'por_pais': desglose,
        }
        if corte:
            item['contando_desde'] = str(corte)
        # Aviso proactivo: que Jarvis lo diga sin que se lo pregunten.
        if restante < 0:
            item['alerta'] = 'SALDO NEGATIVO — el proveedor puede cortar el servicio.'
        elif depositado and restante < depositado * 0.15:
            item['alerta'] = f'Queda menos del 15% del saldo (${restante}). Conviene recargar.'
        salida.append(item)

    return {'proveedores': salida}


# ══════════════════════════════════════════════════════════════════
#  GRÁFICAS
# ══════════════════════════════════════════════════════════════════

_TIPOS_GRAFICO = ('barras', 'lineas', 'area', 'torta')


@skill(
    name='mostrar_grafico',
    description=(
        'Muestra un gráfico en la pantalla del operador. Usala SIEMPRE que los datos '
        'se entiendan mejor viéndolos (evolución en el tiempo, comparaciones entre '
        'países, distribución por hora). Primero conseguí los datos con otra skill y '
        'después pasálos acá. Los datos van como lista de objetos con las claves que '
        'se indiquen en eje_x y series.'),
    params={
        'titulo': {'type': 'string', 'description': 'Título del gráfico'},
        'tipo': {'type': 'string', 'description': 'barras, lineas, area o torta'},
        'datos': {'type': 'array', 'items': {'type': 'object'},
                  'description': 'Lista de filas, ej: [{"hora":10,"llamadas":30}, ...]'},
        'eje_x': {'type': 'string', 'description': 'Clave de los datos que va en el eje X'},
        'series': {'type': 'array', 'items': {'type': 'string'},
                   'description': 'Claves numéricas a graficar, ej: ["llamadas","contestadas"]'},
        'nota': {'type': 'string', 'description': 'Opcional: aclaración al pie del gráfico'},
    },
    required=['titulo', 'tipo', 'datos', 'eje_x', 'series'],
    category=READ,
    examples=['mostrame el gráfico', 'graficá la tendencia', 'quiero verlo en pantalla'],
)
def mostrar_grafico(titulo, tipo, datos, eje_x, series, ctx, nota=None):
    tipo = (tipo or '').strip().lower()
    if tipo not in _TIPOS_GRAFICO:
        raise SkillError(f'tipo de gráfico inválido: {tipo!r}. Usá: {", ".join(_TIPOS_GRAFICO)}')
    if not isinstance(datos, list) or not datos:
        raise SkillError('el gráfico necesita al menos una fila de datos')
    if not isinstance(series, list) or not series:
        raise SkillError('hay que indicar al menos una serie a graficar')

    # Validar que las claves existan de verdad — si el modelo inventa un
    # nombre de columna, mejor decírselo ahora que dibujar un gráfico vacío.
    faltantes = [k for k in [eje_x] + list(series) if k not in datos[0]]
    if faltantes:
        disponibles = ', '.join(datos[0].keys())
        raise SkillError(
            f'estas claves no están en los datos: {faltantes}. Disponibles: {disponibles}')

    limpio = []
    for fila in datos[:200]:      # techo defensivo: nadie lee 500 barras
        nueva = {eje_x: fila.get(eje_x)}
        for s in series:
            valor = fila.get(s)
            try:
                nueva[s] = float(valor) if valor is not None else 0.0
            except (TypeError, ValueError):
                nueva[s] = 0.0
        limpio.append(nueva)

    # ctx.charts lo lee el servidor después de la respuesta para
    # mandárselo al frontend. La skill no dibuja; solo declara.
    spec = {'titulo': titulo, 'tipo': tipo, 'datos': limpio,
            'eje_x': eje_x, 'series': series, 'nota': nota}
    ctx.charts.append(spec)

    return {
        'mostrado': True, 'titulo': titulo, 'tipo': tipo,
        'filas': len(limpio),
        'nota_para_ti': ('El gráfico ya está en pantalla. En tu respuesta hablada NO leas '
                         'los números uno por uno: contá qué se ve y qué significa.'),
    }


# ══════════════════════════════════════════════════════════════════
#  MEMORIA Y APRENDIZAJE
# ══════════════════════════════════════════════════════════════════

@skill(
    name='recordar',
    description=(
        'Guarda un dato en la memoria permanente, para tenerlo disponible en futuras '
        'conversaciones. Usala cuando el operador diga "acordate de esto", "recordá que..." '
        'o te dé información que claramente va a servir después.'),
    params={
        'tema': {'type': 'string', 'description': 'Etiqueta corta del recuerdo, ej: "objetivo mensual"'},
        'contenido': {'type': 'string', 'description': 'Qué recordar, en una o dos frases'},
    },
    required=['tema', 'contenido'],
    category=WRITE,
    confirm_prompt=None,   # guardar una nota es inocuo: no pide confirmación
    examples=['acordate que la meta es 50 cuentas por semana',
              'recordá que los viernes cerramos antes'],
)
def recordar(tema, contenido, ctx):
    tema = (tema or '').strip()[:80]
    contenido = (contenido or '').strip()[:1000]
    if not tema or not contenido:
        raise SkillError('necesito un tema y un contenido para recordar')
    ctx.jdb.execute("""INSERT INTO memories (topic, content) VALUES (§,§)
                       ON CONFLICT(topic) DO UPDATE SET
                       content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
                    (tema, contenido))
    return {'guardado': True, 'tema': tema}


@skill(
    name='consultar_memoria',
    description='Lista todo lo que Jarvis tiene guardado en su memoria permanente.',
    params={},
    required=[],
    category=READ,
    examples=['qué te acordás', 'qué tenés guardado'],
)
def consultar_memoria(ctx):
    filas = ctx.jdb.q("SELECT topic, content, updated_at FROM memories ORDER BY updated_at DESC")
    return {'recuerdos': [{'tema': f['topic'], 'contenido': f['content'],
                           'actualizado': f['updated_at']} for f in filas]}


@skill(
    name='olvidar',
    description='Borra un recuerdo de la memoria permanente por su tema.',
    params={'tema': {'type': 'string', 'description': 'Tema exacto del recuerdo a borrar'}},
    required=['tema'],
    category=WRITE,
    confirm_prompt='Voy a borrar el recuerdo "{tema}". ¿Confirmás?',
    examples=['olvidá lo de la meta mensual'],
)
def olvidar(tema, ctx):
    _, n = ctx.jdb.execute("DELETE FROM memories WHERE topic = §", (tema,))
    if not n:
        raise SkillError(f'no tenía nada guardado bajo el tema "{tema}"')
    return {'borrado': True, 'tema': tema}


@skill(
    name='aprender_correccion',
    description=(
        'Guarda una corrección del operador para no repetir el error. Usala cuando te '
        'digan que interpretaste algo mal, que preferís otra forma de responder, o que '
        'un término significa algo específico en este negocio. Estas lecciones se '
        'aplican en todas las conversaciones siguientes.'),
    params={
        'leccion': {'type': 'string', 'description': 'La regla aprendida, en imperativo. Ej: "cuando pregunten por cuentas, mostrar siempre el desglose por país"'},
        'contexto': {'type': 'string', 'description': 'Opcional: qué situación disparó la corrección'},
    },
    required=['leccion'],
    category=WRITE,
    confirm_prompt=None,
    examples=['no, cuando digo cuentas me refiero a conversiones',
              'la próxima vez mostrame también el gráfico'],
)
def aprender_correccion(leccion, ctx, contexto=None):
    leccion = (leccion or '').strip()[:500]
    if not leccion:
        raise SkillError('la lección no puede estar vacía')

    # Evitar duplicados casi idénticos: la memoria de lecciones se
    # inyecta en cada prompt, así que si se llena de repeticiones el
    # contexto se degrada y Jarvis empeora en vez de mejorar.
    existentes = ctx.jdb.q("SELECT id, lesson FROM lessons")
    normal = ' '.join(leccion.lower().split())
    for e in existentes:
        if ' '.join(e['lesson'].lower().split()) == normal:
            return {'guardado': False, 'motivo': 'ya tenía esa lección aprendida'}

    ctx.jdb.execute("INSERT INTO lessons (lesson, context) VALUES (§,§)",
                    (leccion, (contexto or '')[:500]))
    return {'guardado': True, 'leccion': leccion}


@skill(
    name='diagnostico_jarvis',
    description=(
        'Auto-diagnóstico: qué skills viene usando Jarvis, cuáles fallan, cuántas '
        'lecciones aprendió y qué integraciones están caídas. Para cuando el operador '
        'pregunta cómo está funcionando el sistema o algo no anda.'),
    params={},
    required=[],
    category=READ,
    examples=['cómo venís funcionando', 'algo está fallando', 'diagnóstico'],
)
def diagnostico_jarvis(ctx):
    uso = ctx.jdb.q("""
        SELECT skill, COUNT(*) AS veces,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS fallos,
               CAST(AVG(ms) AS INTEGER) AS ms_promedio
        FROM skill_runs WHERE created_at >= datetime('now', '-7 days')
        GROUP BY skill ORDER BY veces DESC LIMIT 15""")

    errores = ctx.jdb.q("""
        SELECT skill, error, created_at FROM skill_runs
        WHERE ok = 0 AND created_at >= datetime('now', '-24 hours')
        ORDER BY id DESC LIMIT 5""")

    lecciones = ctx.jdb.one("SELECT COUNT(*) AS n FROM lessons")
    recuerdos = ctx.jdb.one("SELECT COUNT(*) AS n FROM memories")
    acciones = ctx.jdb.q("""SELECT skill, created_at FROM audit_log
                            ORDER BY id DESC LIMIT 5""")

    return {
        'uso_ultimos_7_dias': [{'skill': u['skill'], 'veces': u['veces'],
                                'fallos': u['fallos'], 'ms_promedio': u['ms_promedio']}
                               for u in uso],
        'errores_ultimas_24h': [{'skill': e['skill'], 'error': (e['error'] or '')[:200],
                                 'cuando': e['created_at']} for e in errores],
        'lecciones_aprendidas': int(lecciones.get('n') or 0),
        'recuerdos_guardados': int(recuerdos.get('n') or 0),
        'ultimas_acciones': [{'skill': a['skill'], 'cuando': a['created_at']} for a in acciones],
    }
