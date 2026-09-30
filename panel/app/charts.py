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
        })
        cy = pad_t + plot_h - (plot_h * (r['cost'] or 0) / c_max)
        pts.append((round(x + bw / 2, 2), round(cy, 2)))

    cost_path = 'M' + ' L'.join(f'{x},{y}' for x, y in pts)
    grid = [{'y': round(pad_t + plot_h - plot_h * i / 4, 2),
             'v': int(y_max * i / 4)} for i in range(5)]

    return {'empty': False, 'w': round(w, 2), 'h': h, 'bars': bars,
            'cost_path': cost_path, 'y_max': int(y_max), 'cost_max': round(c_max, 2),
            'gridlines': grid, 'plot_h': plot_h, 'pad_t': pad_t}


def hourly(rows, w=520, h=150, pad_b=22, pad_t=8):
    """Perfil por hora en IST. Solo muestra horas con actividad + margen."""
    active = [r for r in rows if r['total_calls'] > 0]
    if not active:
        return {'empty': True, 'bars': [], 'w': w, 'h': h}
    lo = max(0, min(r['hour'] for r in active) - 1)
    hi = min(23, max(r['hour'] for r in active) + 1)
    sel = [r for r in rows if lo <= r['hour'] <= hi]

    plot_h = h - pad_b - pad_t
    y_max = _nice_max(max(r['total_calls'] for r in sel))
    n = len(sel)
    gap = 4
    bw = max(2.0, (w - (n - 1) * gap) / n)
    peak = max(r['total_calls'] for r in sel)

    bars = []
    for i, r in enumerate(sel):
        bh = plot_h * r['total_calls'] / y_max if y_max else 0
        ah = plot_h * r['answered'] / y_max if y_max else 0
        bars.append({
            'x': round(i * (bw + gap), 2), 'w': round(bw, 2),
            'y': round(pad_t + plot_h - bh, 2), 'h': round(bh, 2),
            'ay': round(pad_t + plot_h - ah, 2), 'ah': round(ah, 2),
            'hour': r['hour'], 'total': r['total_calls'],
            'answered': r['answered'], 'asr': r['asr'], 'cost': r['cost'],
            'peak': r['total_calls'] == peak,
        })
    return {'empty': False, 'bars': bars, 'w': round(w, 2), 'h': h,
            'y_max': int(y_max), 'baseline': round(pad_t + plot_h, 2)}


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
        {'key': 'attempts', 'label': 'Intentos de llamada', 'v': attempts,
         'note': 'marcaciones enviadas al proveedor'},
        {'key': 'answered', 'label': 'Contestadas', 'v': answered,
         'note': 'alguien descolgó'},
        {'key': 'real', 'label': 'Conversación real', 'v': real,
         'note': 'más de 30 segundos hablando'},
        {'key': 'account', 'label': 'Cuenta abierta', 'v': accounts,
         'note': 'registrada en Atlantis'},
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
