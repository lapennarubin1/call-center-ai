"""
Geometría de gráficos — se calcula en Python (testeable) y se dibuja
como SVG en la plantilla. Sin librerías de gráficos ni CDN externo:
el panel funciona aunque no haya internet en el navegador.
"""


def _nice_max(v, steps=4):
    """Redondea el techo del eje a un número legible."""
    if v <= 0:
        return steps
    import math
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if mag * m >= v:
            return mag * m
    return mag * 10


def trace(series, w=1000, h=190, pad_b=26, pad_t=10):
    """
    Traza de señal: una barra por punto. La barra completa es el total de
    intentos; la porción llena de abajo son las contestadas. Encima, la
    línea de costo. Es la lectura de un vistazo: volumen, efectividad y gasto.

    'attempts' y 'answered' comparten la escala de conteo (eje izquierdo).
    'cost' vive en su propia escala en dólares — no comparte pixel-espacio
    numéricamente con las barras aunque se dibuje superpuesto. Por eso solo
    las barras necesitan reescalarse dinámicamente entre sí cuando se
    apaga una de las dos (ver recalcScale en el JS): el costo, al estar
    solo en su eje, no necesita reescala propia al togglear las barras.
    """
    if not series:
        return {'empty': True, 'w': w, 'h': h, 'bars': [], 'cost_path': '',
                'y_max': 0, 'cost_max': 0, 'gridlines': []}

    n = len(series)
    plot_h = h - pad_b - pad_t
    y_max = _nice_max(max(r['total_calls'] for r in series))
    c_max = _nice_max(max(r['cost'] for r in series)) or 1

    gap = 2 if n > 60 else 3 if n > 30 else 5
    bw = max(1.0, (w - (n - 1) * gap) / n)

    bars, pts = [], []
    for i, r in enumerate(series):
        x = i * (bw + gap)
        tot = r['total_calls'] or 0
        ans = r['answered'] or 0
        bh = plot_h * tot / y_max if y_max else 0
        ah = plot_h * ans / y_max if y_max else 0
        bars.append({
            'x': round(x, 2), 'w': round(bw, 2),
            'y': round(pad_t + plot_h - bh, 2), 'h': round(bh, 2),
            'ay': round(pad_t + plot_h - ah, 2), 'ah': round(ah, 2),
            'label': r['bucket'], 'total': tot, 'answered': ans,
            'asr': r['asr'], 'cost': r['cost'], 'minutes': r['billed_minutes'],
            'cx': round(x + bw / 2, 2),
        })
        cy = pad_t + plot_h - (plot_h * (r['cost'] or 0) / c_max)
        pts.append({'x': round(x + bw / 2, 2), 'y': round(cy, 2), 'v': r['cost'] or 0})

    cost_path = 'M' + ' L'.join(f"{p['x']},{p['y']}" for p in pts)
    grid = [{'y': round(pad_t + plot_h - plot_h * i / 4, 2),
             'v': int(y_max * i / 4)} for i in range(5)]

    # raw_points para recalcular todo en el navegador al togglear series —
    # mismo patrón que funnel_trend(). 'attempts_raw'/'answered_raw' usan
    # el conteo real (no la altura en px); 'cost_raw' usa el $ real.
    attempts_raw = [{'x': b['cx'], 'v': b['total']} for b in bars]
    answered_raw = [{'x': b['cx'], 'v': b['answered']} for b in bars]
    cost_raw = [{'x': p['x'], 'v': p['v']} for p in pts]

    # Labels visibles en cada punto solo si no son demasiados (≤12,
    # mismo umbral que funnel_trend) — si no, solo el último valor.
    show_all_labels = n <= 12
    if show_all_labels:
        for i, b in enumerate(bars):
            b['anchor'] = 'start' if i == 0 else 'end' if i == n - 1 else 'middle'
    last_bar = bars[-1]
    last_cost = pts[-1]

    return {'empty': False, 'w': round(w, 2), 'h': h, 'bars': bars,
            'cost_path': cost_path, 'cost_points': pts,
            'y_max': int(y_max), 'cost_max': round(c_max, 2),
            'gridlines': grid, 'plot_h': plot_h, 'pad_t': pad_t, 'pad_b': pad_b,
            'attempts_raw': attempts_raw, 'answered_raw': answered_raw, 'cost_raw': cost_raw,
            'show_all_labels': show_all_labels,
            'last_bar': last_bar, 'last_cost': last_cost}


def hourly(rows, w=1180, h=280, pad_b=26, pad_t=10, pad_l=30):
    """
    Perfil por hora en hora LOCAL del país — 3 barras separadas por hora
    (attempts, answered, accounts), agrupadas lado a lado, togglables.
    'attempts' y 'answered' comparten el eje de conteo (igual que en
    trace()); 'accounts' (cuentas abiertas) vive en su propio eje,
    independiente, para que su barra tenga una altura significativa por
    sí sola en vez de aplastarse contra el piso al lado de conteos en
    cientos.

    Dinámico: 'rows' ya viene recortado por analytics.hourly_profile()
    al tramo de horas con actividad real (ej. 10-17), no las 24 horas
    fijas del día — así el ritmo del día se lee con la escala aprovechada
    en vez de un bloque casi vacío con un puñado de barras al medio.

    pad_l reserva margen real a la izquierda para los valores del eje Y
    (en vez de texto fuera del viewBox, que un navegador puede recortar).
    """
    if not rows:
        return {'empty': True, 'w': w, 'h': h}

    sel = rows

    plot_h = h - pad_b - pad_t
    plot_w = w - pad_l
    y_max = _nice_max(max(r['total_calls'] for r in sel)) or 1
    a_max = _nice_max(max(r['accounts'] or 0 for r in sel)) or 1
    peak = max(r['total_calls'] for r in sel)

    n = len(sel)
    group_gap = 5
    sub_gap = 1.5
    gw = max(10.0, (plot_w - (n - 1) * group_gap) / n)   # ancho del grupo (hora)
    sw = max(2.0, (gw - 2 * sub_gap) / 3)                 # ancho de cada sub-barra

    def sub_h(v, vmax):
        return plot_h * min(v, vmax) / vmax if vmax else 0

    groups, attempts_bars, answered_bars, accounts_bars = [], [], [], []
    for i, r in enumerate(sel):
        gx = pad_l + i * (gw + group_gap)
        is_peak = r['total_calls'] == peak and peak > 0

        ah = sub_h(r['total_calls'], y_max)
        nh = sub_h(r['answered'], y_max)
        ch = sub_h(r['accounts'] or 0, a_max)

        x_attempts = gx
        x_answered = gx + sw + sub_gap
        x_accounts = gx + 2 * (sw + sub_gap)

        attempts_bars.append({'x': round(x_attempts, 2), 'w': round(sw, 2),
                               'y': round(pad_t + plot_h - ah, 2), 'h': round(ah, 2),
                               'raw': r['total_calls'], 'peak': is_peak})
        answered_bars.append({'x': round(x_answered, 2), 'w': round(sw, 2),
                               'y': round(pad_t + plot_h - nh, 2), 'h': round(nh, 2),
                               'raw': r['answered'], 'peak': is_peak,
                               'cx': round(x_answered + sw / 2, 2), 'asr': r['asr']})
        accounts_bars.append({'x': round(x_accounts, 2), 'w': round(sw, 2),
                               'y': round(pad_t + plot_h - ch, 2), 'h': round(ch, 2),
                               'raw': r['accounts'] or 0, 'peak': is_peak})

        groups.append({'x': round(gx, 2), 'w': round(gw, 2), 'cx': round(gx + gw / 2, 2),
                        'hour': r['hour'], 'total': r['total_calls'], 'answered': r['answered'],
                        'asr': r['asr'], 'accounts': r['accounts'] or 0, 'peak': is_peak,
                        'has_data': r['total_calls'] > 0})

    grid = [{'y': round(pad_t + plot_h - plot_h * i / 4, 2), 'v': int(y_max * i / 4)} for i in range(5)]

    return {'empty': False, 'w': round(w, 2), 'h': h, 'pad_l': pad_l, 'groups': groups,
            'attempts_bars': attempts_bars, 'answered_bars': answered_bars, 'accounts_bars': accounts_bars,
            'y_max': int(y_max), 'accounts_max': round(a_max, 2), 'gridlines': grid,
            'plot_h': plot_h, 'pad_t': pad_t, 'pad_b': pad_b,
            'baseline': round(pad_t + plot_h, 2), 'show_all_labels': True,
            'hour_lo': sel[0]['hour'], 'hour_hi': sel[-1]['hour']}


def ladder(traffic, durations, fn):
    """
    Escalera del embudo: intentos → contestadas → conversación real → cuenta.
    Cada peldaño se dibuja proporcional al anterior, que es como realmente
    se pierde el volumen. Es la historia del negocio en una sola figura.
    """
    attempts  = traffic['total_calls']
    answered  = traffic['answered']
    real      = durations['meaningful']
    accounts  = fn.get('accounts_opened', 0) if fn.get('available') else 0

    steps = [
        {'key': 'attempts', 'label': 'Call attempts', 'v': attempts,
         'note': 'dials sent to provider'},
        {'key': 'answered', 'label': 'Answered', 'v': answered,
         'note': 'someone answered'},
        {'key': 'real', 'label': 'Real conversation', 'v': real,
         'note': 'more than 60 seconds talking'},
        {'key': 'account', 'label': 'Account opened', 'v': accounts,
         'note': 'registered in Atlantis'},
    ]
    base = attempts or 1
    for i, s in enumerate(steps):
        s['pct_total'] = round(s['v'] / base * 100, 2)
        s['width'] = max(1.2, s['v'] / base * 100)
        prev = steps[i - 1]['v'] if i else None
        s['pct_prev'] = round(s['v'] / prev * 100, 1) if prev else None
        s['drop'] = (prev - s['v']) if prev is not None else None
    return steps


def donut(items, size=132, thickness=17):
    """Anillo de disposiciones. items: [{label, value, tone}]"""
    total = sum(i['value'] for i in items) or 1
    r = (size - thickness) / 2
    cx = cy = size / 2
    circ = 2 * 3.141592653589793 * r
    out, off = [], 0.0
    for it in items:
        frac = it['value'] / total
        seg = circ * frac
        out.append({**it,
                    'dash': f'{seg:.2f} {circ - seg:.2f}',
                    'offset': f'{-off:.2f}',
                    'pct': round(frac * 100, 1)})
        off += seg
    return {'segments': out, 'r': round(r, 2), 'cx': cx, 'cy': cy,
            'thickness': thickness, 'size': size, 'total': total}


def bars_h(items, key='count', label='label'):
    """Barras horizontales normalizadas (duración, reintentos, pipeline)."""
    mx = max((i[key] for i in items), default=0) or 1
    return [{**i, 'w': round(i[key] / mx * 100, 2)} for i in items]


def funnel_line(steps, w=1000, h=190, pad_b=26, pad_t=24):
    """
    Line chart clásico: un punto por etapa, conectados por una diagonal recta
    (estilo 'spline'), con el valor en % marcado arriba de cada punto —
    igual al gráfico de referencia (línea de color, puntos grandes, label
    encima de cada uno).
    """
    if not steps:
        return {'empty': True, 'w': w, 'h': h, 'points': [], 'path': '', 'gridlines': []}
    n = len(steps)
    plot_h = h - pad_b - pad_t
    xs = [i * w / (n - 1) for i in range(n)] if n > 1 else [w / 2]

    pts = []
    for i, s in enumerate(steps):
        pct = s['pct_total']
        y = pad_t + plot_h - (plot_h * pct / 100)
        label_y = y - 14 if y - 14 > 10 else y + 22
        pts.append({
            'x': round(xs[i], 2), 'y': round(y, 2),
            'label': s['label'], 'note': s['note'],
            'v': s['v'], 'pct': pct, 'label_y': round(label_y, 2),
        })

    path = 'M' + ' L'.join(f"{p['x']},{p['y']}" for p in pts)

    grid = [{'y': round(pad_t + plot_h - plot_h * i / 4, 2), 'v': i * 25} for i in range(5)]

    return {'empty': False, 'w': w, 'h': h, 'points': pts, 'path': path,
            'gridlines': grid, 'plot_h': plot_h, 'pad_t': pad_t}


def _bucket_label(bucket, grain):
    """'2026-08-11 13:00' -> '13:00' | '2026-08-11' -> 'Aug 11' | etc."""
    from datetime import datetime as _dt
    try:
        if grain in ('hour', '30min'):
            return bucket.split(' ')[1][:5]
        if grain == 'day':
            return _dt.strptime(bucket, '%Y-%m-%d').strftime('%b %d')
        if grain == 'month':
            return _dt.strptime(bucket, '%Y-%m').strftime('%b %Y')
        return bucket  # week: 'YYYY-Www' se deja tal cual
    except Exception:
        return bucket


def funnel_trend(rows, grain='hour', w=1000, h=250, pad_b=8, pad_t=12):
    """
    3 líneas en el tiempo (Answered / Real conversation / Account opened).
    Escala del eje Y auto-ajustada al máximo real de los datos (como el
    resto de los gráficos del panel) — NO fija a 0-100%, porque estos %
    reales rondan 0-15%: con escala fija a 100 todo el movimiento queda
    apachurrado contra el piso y se ve como una línea plana.
    Los labels del último punto se apilan verticalmente por orden de
    valor para que nunca se superpongan entre sí, aunque sean casi iguales.
    """
    if not rows:
        return {'empty': True, 'w': w, 'h': h}
    n = len(rows)
    plot_h = h - pad_b - pad_t
    xs = [i * w / (n - 1) for i in range(n)] if n > 1 else [w / 2]

    y_max = _nice_max(max(
        (r['pct_answered'] for r in rows), default=0,
    ), steps=4)
    y_max = max(y_max, _nice_max(max((r['pct_real_conv'] for r in rows), default=0)))
    y_max = max(y_max, _nice_max(max((r['pct_accounts'] for r in rows), default=0)))
    y_max = max(y_max, 1)  # nunca 0, evita división por cero si todo es 0%

    def y_of(pct_val):
        return round(pad_t + plot_h - (plot_h * min(pct_val, y_max) / y_max), 2)

    series = {
        'answered' : {'key': 'pct_answered',  'label': 'Answered',          'css': 'ft-answered'},
        'real_conv': {'key': 'pct_real_conv', 'label': 'Real conversation', 'css': 'ft-realconv'},
        'accounts' : {'key': 'pct_accounts',  'label': 'Account opened',    'css': 'ft-accounts'},
    }
    for s in series.values():
        pts = [{'x': round(xs[i], 2), 'y': y_of(r[s['key']]), 'pct': r[s['key']]} for i, r in enumerate(rows)]
        s['path'] = 'M' + ' L'.join(f"{p['x']},{p['y']}" for p in pts)
        s['points'] = pts
        # Puntos crudos (x, pct) para que el navegador pueda recalcular la
        # escala Y y redibujar sin pedirle nada de nuevo al servidor cuando
        # el usuario apaga/prende líneas con los botones de toggle.
        s['raw_points'] = [{'x': p['x'], 'pct': p['pct']} for p in pts]
        # Slug sin el prefijo 'ft-' (ej. 'ft-realconv' -> 'realconv') — es
        # el mismo string que usan los botones de toggle en onclick(), así
        # el JS encuentra el grupo correcto por data-series.
        s['slug'] = s['css'][3:]

    def resolve_labels(idx):
        """
        Posición Y de los 3 labels (uno por serie) en un mismo punto de
        tiempo, sin que se superpongan entre sí. Solo empuja hacia abajo
        a los que realmente están pegados — el resto conserva su
        posición natural (pegada a su propio punto). Si el stacking se
        pasa del piso del gráfico (3 valores pegados cerca de 0%), se
        sube TODO el grupo en bloque — nunca se deja un label cortado
        fuera del viewBox.
        """
        natural = {}
        for key, s in series.items():
            p = s['points'][idx]
            natural[key] = p['y'] - 10 if p['y'] - 10 > 10 else p['y'] + 16
        min_gap = 17
        order = sorted(natural.keys(), key=lambda k: natural[k])  # arriba → abajo
        resolved, prev_y = {}, None
        for key in order:
            y = natural[key]
            if prev_y is not None and y < prev_y + min_gap:
                y = prev_y + min_gap
            resolved[key] = round(y, 2)
            prev_y = y
        overflow = max(resolved.values()) - (h - 4)
        if overflow > 0:
            resolved = {k: round(v - overflow, 2) for k, v in resolved.items()}
        return resolved

    # Con pocos puntos (Today/Yesterday parcial, Week) se puede mostrar el
    # % en CADA hora/día sin amontonar. Con muchos (24h completas, 30
    # días) solo se marca el último, como antes.
    show_all_labels = n <= 12
    if show_all_labels:
        for i in range(n):
            resolved = resolve_labels(i)
            anchor = 'start' if i == 0 else 'end' if i == n - 1 else 'middle'
            for key, s in series.items():
                s['points'][i]['label_y'] = resolved[key]
                s['points'][i]['anchor'] = anchor
    else:
        resolved = resolve_labels(n - 1)
        for key, s in series.items():
            s['last_label_y'] = resolved[key]

    cols = []
    for i, r in enumerate(rows):
        cols.append({
            'x': round(xs[i], 2),
            'label': _bucket_label(r['bucket'], grain),
            'attempts': r['attempts'], 'answered': r['answered'],
            'real_conv': r['real_conv'], 'accounts': r['accounts'],
            'pct_answered': r['pct_answered'], 'pct_real_conv': r['pct_real_conv'],
            'pct_accounts': r['pct_accounts'],
        })
    col_w = w / n

    # Ticks del eje X: todas las horas si son pocas, si no cada 2/3/N para
    # no amontonar texto (igual criterio que usan gráficos de calendario).
    max_ticks = 12
    step = max(1, round(n / max_ticks))
    ticks = [cols[i] for i in range(0, n, step)]
    if cols[-1] not in ticks:
        ticks.append(cols[-1])

    grid = [{'y': round(pad_t + plot_h - plot_h * i / 4, 2), 'v': round(y_max * i / 4, 1)} for i in range(5)]

    return {
        'empty': False, 'w': w, 'h': h, 'series': series, 'cols': cols,
        'col_w': round(col_w, 2), 'gridlines': grid, 'ticks': ticks, 'y_max': y_max,
        'show_all_labels': show_all_labels, 'pad_t': pad_t, 'pad_b': pad_b,
    }
