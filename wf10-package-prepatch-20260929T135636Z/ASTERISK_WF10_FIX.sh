#!/usr/bin/env bash
# =============================================================================
# ASTERISK_WF10_FIX.sh — productor durable de grabaciones Asterisk para WF10
#
# CAUSA (verificada en Asterisk 20 real): System(wf10-enqueue.py) está DESPUÉS de
# Dial(). En una llamada CONTESTADA, cuando cuelga cualquiera de las dos partes el
# canal termina dentro de Dial() y las prioridades siguientes (StopMixMonitor,
# System) NO se ejecutan. System() sólo corre en NOANSWER/BUSY (WAV de 44 bytes,
# que el enqueue descarta). Por eso casi ninguna contestada llega a la cola.
#
# FIX: agregar el 3.er argumento de MixMonitor (post-proceso). Asterisk lo ejecuta
# SIEMPRE, cuando termina la grabación y DESPUÉS de cerrar el WAV, sin importar
# quién cuelga. Se modifica la línea MixMonitor existente EN SU LUGAR: no se
# insertan prioridades (insertar una línea antes de Dial con llamadas activas hace
# que, al volver Dial, el canal ejecute otra vez Dial = re-marcar al cliente).
#   MixMonitor(${RECORDING},b)
#   -> MixMonitor(${RECORDING},b,/usr/local/bin/wf10-mixmon-post.sh <mismos args del System, DIALSTATUS=ANSWER>)
# wf10-mixmon-post.sh sólo encola si el WAV tiene audio (> 44 bytes). Con la opción
# 'b' MixMonitor graba únicamente mientras el canal está puenteado, o sea, contestado.
# Llama al MISMO wf10-enqueue.py (idempotente por UNIQUEID): un único productor.
#
# USO (como root en el servidor Asterisk):
#   ./ASTERISK_WF10_FIX.sh check        # solo lectura: muestra exactamente qué cambiaría
#   ./ASTERISK_WF10_FIX.sh apply        # backup + cambio + dialplan reload + verificación (rollback automático si falla)
#   ./ASTERISK_WF10_FIX.sh status       # estado actual
#   ./ASTERISK_WF10_FIX.sh finalize     # DESPUÉS de validar: desactiva el cron viejo send_recordings.sh (con backup)
#   ./ASTERISK_WF10_FIX.sh rollback [DIR]  # restaura dialplan (+ crontab si se finalizó) desde el backup
# Nunca reinicia Asterisk: sólo "dialplan reload" (no corta llamadas activas).
# =============================================================================
set -euo pipefail
ACTION="${1:-check}"
FORCE="${FORCE:-0}"
POST=/usr/local/bin/wf10-mixmon-post.sh
ENQUEUE=/usr/local/bin/wf10-enqueue.py
ENQ_LOG=/var/log/asterisk/wf10-enqueue.log
QUEUE_DIR=/var/spool/wf10/queue
BACKUP_ROOT=/root/wf10-fix-backups
AST_ETC=/etc/asterisk
CRON_MATCH='send_recordings.sh'

die() { echo "❌ $*" >&2; exit 1; }
info() { echo "• $*"; }
[ "$(id -u)" = "0" ] || die "ejecutar como root"
command -v asterisk >/dev/null || die "no se encontró el binario asterisk"
command -v python3 >/dev/null || die "se necesita python3"
AST_UP=1; asterisk -rx "core show version" >/dev/null 2>&1 || AST_UP=0
[ "$AST_UP" = "1" ] || [ "$ACTION" = "rollback" ] || die "Asterisk no está corriendo o no responde a 'asterisk -rx'"

AST_USER="$( (ps -o user= -C asterisk 2>/dev/null || true) | head -1 | tr -d ' ')"; AST_USER="${AST_USER:-asterisk}"

# ---------------------------------------------------------------------------
# Plan: descubre los contextos WF10 y calcula el cambio exacto (no escribe nada)
# ---------------------------------------------------------------------------
plan() {
  asterisk -rx "dialplan show" > "$1/dialplan-show.txt"
  python3 - "$1" "$AST_ETC" "$POST" "$ENQUEUE" "$FORCE" <<'PY'
import json, os, re, sys, hashlib
out_dir, ast_etc, post, enqueue, force = sys.argv[1:6]
txt = open(os.path.join(out_dir, 'dialplan-show.txt'), encoding='utf-8', errors='replace').read()
ctx = ext = None
prios = []  # (ctx, ext, prio, app, file, line)
for raw in txt.splitlines():
    m = re.match(r"^\[ Context '([^']+)' created by '([^']+)' \]", raw)
    if m: ctx = m.group(1); ext = None; continue
    m = re.match(r"^\s+'([^']+)' =>\s+(?:\[[^\]]*\]\s+)?(\d+)\.\s+(.*?)\s+\[([^\]]+)\]\s*$", raw) or re.match(r"^\s+(?:\[[^\]]*\]\s+)?(\d+)\.\s+(.*?)\s+\[([^\]]+)\]\s*$", raw)
    if not m or ctx is None: continue
    if len(m.groups()) == 4: ext, p, app, src = m.groups()
    else: p, app, src = m.groups()
    prios.append((ctx, ext, int(p), app.strip(), src))
counts = re.search(r"-= (\d+) extensions? \((\d+) priorit(?:y|ies)\) in (\d+) contexts?\. =-", txt)
# Ediciones en disco que todavía NO están cargadas: "dialplan reload" también las activaría.
# Se compara cada prioridad cargada (archivo:línea) con el texto actual de esa línea en disco.
drift, cache = [], {}
norm = lambda s: re.sub(r'\s+', '', s)
for c, e, p, app, src in prios:
    m = re.match(r"(.+):(\d+)$", src)
    if not m: continue
    f = m.group(1) if m.group(1).startswith('/') else os.path.join(ast_etc, m.group(1))
    if f not in cache:
        try: cache[f] = open(f, encoding='utf-8', errors='replace').read().split('\n')
        except Exception: cache[f] = None
    lines = cache[f]; ln = int(m.group(2))
    disk = lines[ln - 1] if lines and ln <= len(lines) else None
    a = norm(app)
    if disk is None or (a not in norm(disk) and not (a.endswith('()') and a[:-2] in norm(disk))):
        drift.append(f"{f}:{ln} cargado='{app}' | en disco='{(disk or '(no existe)').strip()}'")
by = {}
for c, e, p, app, src in prios: by.setdefault((c, e), []).append((p, app, src))
targets, errors, already = [], [], []
if counts is None: errors.append("no se pudo leer el total de 'dialplan show' (formato inesperado)")
if drift:
    msg = f"hay {len(drift)} línea(s) del dialplan editadas en disco y NO cargadas; 'dialplan reload' también las activaría: " + ' || '.join(drift[:5])
    if force == '1': print('  ⚠️  (FORCE=1) ' + msg)
    else: errors.append(msg + '  -> revisar/recargar primero, o FORCE=1 si esas ediciones deben activarse')
for (c, e), ps in by.items():
    ps.sort()
    anyp = [x for x in ps if 'wf10-enqueue' in x[1]]
    if not anyp: continue
    sysp = [x for x in anyp if x[1].startswith('System(' + enqueue + ' ')]
    if len(sysp) != len(anyp) or len(sysp) != 1:
        errors.append(f"{c}/{e}: llamada a wf10-enqueue con formato inesperado: {[x[1] for x in anyp]}"); continue
    sp, sapp, _ = sysp[0]
    toks = sapp[len('System(' + enqueue + ' '):-1].split()
    if toks.count('${DIALSTATUS}') != 1: errors.append(f"{c}/{e}: System sin ${{DIALSTATUS}} como argumento: {sapp}"); continue
    mixp = [x for x in ps if x[1].startswith('MixMonitor(') and x[0] < sp]
    if len(mixp) != 1: errors.append(f"{c}/{e}: se esperaba exactamente 1 MixMonitor antes del System (hay {len(mixp)})"); continue
    mp, mapp, msrc = mixp[0]
    dialp = [x for x in ps if x[1].startswith('Dial(') and mp < x[0] < sp]
    if not dialp: errors.append(f"{c}/{e}: no hay Dial() entre MixMonitor y System"); continue
    margs = mapp[len('MixMonitor('):-1]
    parts = [a.strip() for a in margs.split(',')]
    if len(parts) == 3 and parts[2].startswith(post + ' '):
        already.append({'context': c, 'exten': e, 'priority': mp, 'app': mapp}); continue
    if len(parts) != 2: errors.append(f"{c}/{e}: MixMonitor con formato inesperado (se esperaban 2 argumentos): {mapp}"); continue
    wav_expr, opts = parts
    if 'b' not in opts: errors.append(f"{c}/{e}: MixMonitor sin opción 'b' ({mapp}); sin ella un WAV > 44 bytes no prueba que la llamada fue contestada"); continue
    if wav_expr not in toks: errors.append(f"{c}/{e}: el WAV del MixMonitor ({wav_expr}) no es un argumento del System ({sapp})"); continue
    post_toks = ['ANSWER' if t == '${DIALSTATUS}' else t for t in toks]
    cmd = post + ' ' + ' '.join(post_toks)
    if any(ch in cmd for ch in ',|') or cmd.count('(') != cmd.count(')'): errors.append(f"{c}/{e}: argumentos con comas/paréntesis desbalanceados, no seguros dentro de MixMonitor: {cmd}"); continue
    # variables usadas por el comando que se asignan DESPUÉS del MixMonitor -> se evaluarían vacías
    later_sets = set()
    for p, app, _ in ps:
        mm = re.match(r"Set\(([A-Za-z0-9_]+)=", app)
        if mm and p > mp: later_sets.add(mm.group(1))
    used = set(re.findall(r"\$\{([A-Za-z0-9_]+)\}", cmd))
    bad = sorted(used & later_sets)
    if bad: errors.append(f"{c}/{e}: {bad} se asignan después del MixMonitor; el post-proceso se evalúa en el MixMonitor"); continue
    m = re.match(r"(.+):(\d+)$", msrc)
    if not m: errors.append(f"{c}/{e}: la prioridad MixMonitor no informa archivo:línea ({msrc}); ¿dialplan no es extensions.conf?"); continue
    fpath = m.group(1) if m.group(1).startswith('/') else os.path.join(ast_etc, m.group(1))
    lineno = int(m.group(2))
    targets.append({'context': c, 'exten': e, 'priority': mp, 'file': fpath, 'line': lineno, 'old_app': mapp,
                    'new_app': f"MixMonitor({wav_expr},{opts},{cmd})", 'wav_expr': wav_expr, 'opts': opts, 'system': sapp})
# preparar ediciones por archivo, verificando el texto real de cada línea
edits = {}
for t in targets:
    try: lines = open(t['file'], encoding='utf-8').read().split('\n')
    except Exception as ex: errors.append(f"no se puede leer {t['file']}: {ex}"); continue
    if t['line'] > len(lines): errors.append(f"{t['file']}:{t['line']} fuera de rango"); continue
    ln = lines[t['line'] - 1]
    if 'auto-generated by FreePBX' in open(t['file'], encoding='utf-8', errors='replace').read(4000):
        errors.append(f"{t['file']} es generado por FreePBX: el cambio se perdería al regenerar"); continue
    pat = re.compile(r"MixMonitor\(\s*" + re.escape(t['wav_expr']) + r"\s*,\s*" + re.escape(t['opts']) + r"\s*\)")
    if len(pat.findall(ln)) != 1: errors.append(f"{t['file']}:{t['line']} no contiene exactamente 1 '{t['old_app']}': {ln.strip()}"); continue
    new_ln = pat.sub(lambda _: t['new_app'], ln)
    t['old_line'] = ln; t['new_line'] = new_ln
    edits.setdefault(t['file'], {})[t['line']] = new_ln
plan = {'targets': targets, 'already': already, 'errors': errors,
        'counts': {'extensions': int(counts.group(1)), 'priorities': int(counts.group(2)), 'contexts': int(counts.group(3))} if counts else None,
        'files': {f: {'sha256_before': hashlib.sha256(open(f, 'rb').read()).hexdigest()} for f in edits}}
json.dump(plan, open(os.path.join(out_dir, 'plan.json'), 'w'), indent=2, ensure_ascii=False)
for f, ed in edits.items():
    lines = open(f, encoding='utf-8').read().split('\n')
    for ln, new in ed.items(): lines[ln - 1] = new
    open(os.path.join(out_dir, 'new_' + hashlib.sha1(f.encode()).hexdigest()[:10]), 'w', encoding='utf-8').write('\n'.join(lines))
    plan['files'][f]['staged'] = os.path.join(out_dir, 'new_' + hashlib.sha1(f.encode()).hexdigest()[:10])
json.dump(plan, open(os.path.join(out_dir, 'plan.json'), 'w'), indent=2, ensure_ascii=False)
for t in targets:
    print(f"  CAMBIO  [{t['context']}] {t['exten']} prioridad {t['priority']}  ({t['file']}:{t['line']})")
    print(f"     antes:   {t['old_line'].strip()}")
    print(f"     después: {t['new_line'].strip()}")
for a in already: print(f"  YA APLICADO [{a['context']}] {a['exten']} prioridad {a['priority']}")
for e in errors: print(f"  ⚠️  {e}")
if plan['counts']: print(f"  dialplan actual: {plan['counts']['extensions']} extensiones, {plan['counts']['priorities']} prioridades, {plan['counts']['contexts']} contextos")
sys.exit(2 if errors else (3 if not targets else 0))
PY
}

post_script_content() {
cat <<'EOS'
#!/bin/sh
# wf10-mixmon-post.sh — lo ejecuta Asterisk (post-proceso de MixMonitor) cuando termina
# la grabación y el WAV ya está cerrado, en TODAS las llamadas.
# Argumentos: los mismos del System(wf10-enqueue.py ...) del dialplan, con DIALSTATUS=ANSWER.
# Encola sólo si el WAV tiene audio: con la opción 'b' de MixMonitor sólo se graba mientras
# el canal está puenteado (= contestada). WAV de 44 bytes (sólo header) = no contestada.
LOG=/var/log/asterisk/wf10-enqueue.log
WAV=""
for a in "$@"; do case "$a" in *.wav|*.WAV) WAV="$a"; break;; esac; done
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SIZE=0
[ -n "$WAV" ] && [ -f "$WAV" ] && SIZE=$(stat -c %s "$WAV" 2>/dev/null || echo 0)
if [ "$SIZE" -le 44 ]; then
  echo "$TS wf10-mixmon-post args=[$*] size=$SIZE -> sin audio puenteado: no se encola" >> "$LOG" 2>/dev/null
  exit 0
fi
echo "$TS wf10-mixmon-post args=[$*] size=$SIZE -> enqueue" >> "$LOG" 2>/dev/null
timeout 30 /usr/local/bin/wf10-enqueue.py "$@" >> "$LOG" 2>&1
RC=$?
[ "$RC" -ne 0 ] && echo "$TS wf10-mixmon-post enqueue rc=$RC args=[$*]" >> "$LOG" 2>/dev/null
exit 0
EOS
}

# último backup con un apply real que no haya sido revertido
latest_applied() {
  local d
  for d in $(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort -r); do
    d="${d%/}"; [ -f "$d/applied_at" ] && [ ! -f "$d/rolled_back" ] && { echo "$d"; return 0; }
  done
  return 0
}

reload_and_count() {
  asterisk -rx "dialplan reload" >/dev/null
  sleep 1
  asterisk -rx "dialplan show" | grep -E "^-= [0-9]+ extension" || true
}

case "$ACTION" in
  check)
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    echo "== ASTERISK_WF10_FIX: CHECK (sólo lectura) =="
    echo "Asterisk: $(asterisk -rx 'core show version' | head -1)   usuario del proceso: $AST_USER"
    set +e; plan "$T"; RC=$?; set -e
    [ -x "$ENQUEUE" ] && info "$ENQUEUE existe y es ejecutable" || echo "⚠️  $ENQUEUE no existe o no es ejecutable"
    su -s /bin/sh "$AST_USER" -c "test -w '$QUEUE_DIR'" 2>/dev/null && info "$AST_USER puede escribir en $QUEUE_DIR" || echo "⚠️  $AST_USER NO puede escribir en $QUEUE_DIR"
    su -s /bin/sh "$AST_USER" -c "test -w '$ENQ_LOG' || test -w '$(dirname "$ENQ_LOG")'" 2>/dev/null && info "$AST_USER puede escribir $ENQ_LOG" || echo "⚠️  $AST_USER NO puede escribir $ENQ_LOG"
    echo "Llamadas activas: $(asterisk -rx 'core show channels count' | grep -i 'active call' || true)"
    case $RC in 0) echo "✅ CHECK OK: 'apply' haría exactamente los cambios listados.";; 3) echo "ℹ️  Nada que cambiar (ya aplicado o sin contextos WF10).";; *) echo "❌ CHECK con problemas: 'apply' NO modificará nada hasta resolverlos."; exit 2;; esac
    ;;

  apply)
    STAMP=$(date -u +%Y%m%dT%H%M%SZ); B="$BACKUP_ROOT/$STAMP"; mkdir -p "$B"
    echo "== ASTERISK_WF10_FIX: APPLY  (backup en $B) =="
    set +e; plan "$B"; RC=$?; set -e
    [ $RC -eq 3 ] && { rm -rf "$B"; echo "ℹ️  Nada que aplicar."; exit 0; }
    [ $RC -ne 0 ] && { rm -rf "$B"; die "el plan tiene problemas (ver arriba). No se modificó nada."; }
    [ -x "$ENQUEUE" ] || die "$ENQUEUE no existe o no es ejecutable. No se modificó nada."
    if ! su -s /bin/sh "$AST_USER" -c "test -w '$QUEUE_DIR'" 2>/dev/null && [ "$FORCE" != "1" ]; then die "$AST_USER no puede escribir en $QUEUE_DIR (FORCE=1 para ignorar). No se modificó nada."; fi
    # backup de todo lo que se toca
    python3 - "$B" <<'PY'
import json, os, shutil, sys
b = sys.argv[1]; plan = json.load(open(os.path.join(b, 'plan.json')))
os.makedirs(os.path.join(b, 'files'), exist_ok=True)
for f in plan['files']:
    dst = os.path.join(b, 'files', f.lstrip('/')); os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copy2(f, dst)
PY
    [ -e "$POST" ] && cp -a "$POST" "$B/post-script.before" || touch "$B/post-script.absent"
    crontab -l > "$B/crontab.before" 2>/dev/null || true
    date -u +%Y-%m-%dT%H:%M:%SZ > "$B/applied_at"
    # instalar el post-proceso ANTES del reload (las llamadas nuevas ya lo encuentran)
    post_script_content > "$POST.tmp" && chmod 755 "$POST.tmp" && mv -f "$POST.tmp" "$POST"
    sh -n "$POST" || die "el post-script no pasó la verificación de sintaxis"
    # escribir los archivos del dialplan (atómico, preservando dueño y permisos)
    python3 - "$B" <<'PY'
import json, os, shutil, sys, hashlib
b = sys.argv[1]; plan = json.load(open(os.path.join(b, 'plan.json')))
for f, meta in plan['files'].items():
    if hashlib.sha256(open(f, 'rb').read()).hexdigest() != meta['sha256_before']:
        sys.exit(f"{f} cambió mientras se preparaba el plan; abortando")
for f, meta in plan['files'].items():
    st = os.stat(f); tmp = f + '.wf10fix.tmp'
    shutil.copyfile(meta['staged'], tmp); os.chown(tmp, st.st_uid, st.st_gid); os.chmod(tmp, st.st_mode); os.replace(tmp, f)
    meta['sha256_after'] = hashlib.sha256(open(f, 'rb').read()).hexdigest()
json.dump(plan, open(os.path.join(b, 'plan.json'), 'w'), indent=2, ensure_ascii=False)
PY
    BEFORE=$(python3 -c "import json;c=json.load(open('$B/plan.json'))['counts'];print(f\"{c['extensions']} {c['priorities']} {c['contexts']}\")")
    AFTER_LINE=$(reload_and_count)
    AFTER=$(echo "$AFTER_LINE" | sed -E 's/^-= ([0-9]+) extensions? \(([0-9]+) priorit(y|ies)\) in ([0-9]+) contexts?\. =-/\1 \2 \4/')
    OK=1
    [ "$BEFORE" = "$AFTER" ] || { echo "❌ el total del dialplan cambió ($BEFORE -> $AFTER): se esperaba idéntico"; OK=0; }
    asterisk -rx "dialplan show" > "$B/dialplan-show.after.txt"
    python3 - "$B" <<'PY' || OK=0
import json, os, re, sys
from collections import Counter
b = sys.argv[1]; plan = json.load(open(os.path.join(b, 'plan.json')))
def prios(path):
    ctx = ext = None; out = []
    for raw in open(path, encoding='utf-8', errors='replace').read().splitlines():
        m = re.match(r"^\[ Context '([^']+)' created by '([^']+)' \]", raw)
        if m: ctx = m.group(1); ext = None; continue
        m = re.match(r"^\s+'([^']+)' =>\s+(?:\[[^\]]*\]\s+)?(\d+)\.\s+(.*?)\s+\[([^\]]+)\]\s*$", raw) or re.match(r"^\s+(?:\[[^\]]*\]\s+)?(\d+)\.\s+(.*?)\s+\[([^\]]+)\]\s*$", raw)
        if not m or ctx is None: continue
        if len(m.groups()) == 4: ext, p, app, src = m.groups()
        else: p, app, src = m.groups()
        out.append((ctx, ext, int(p), app.strip()))
    return out
before, after = prios(os.path.join(b, 'dialplan-show.txt')), prios(os.path.join(b, 'dialplan-show.after.txt'))
repl = {(t['context'], t['exten'], t['priority']): (t['old_app'], t['new_app']) for t in plan['targets']}
expected = [(c, e, p, repl[(c, e, p)][1] if (c, e, p) in repl and a == repl[(c, e, p)][0] else a) for c, e, p, a in before]
missing, extra = Counter(expected) - Counter(after), Counter(after) - Counter(expected)
for x in list(missing)[:10]: print(f"❌ esperado y no cargado: {x}")
for x in list(extra)[:10]: print(f"❌ cargado y no esperado: {x}")
sys.exit(1 if (missing or extra) else 0)
PY
    if [ "$OK" != "1" ]; then
      echo "↩️  ROLLBACK AUTOMÁTICO"
      python3 - "$B" <<'PY'
import json, os, shutil, sys
b = sys.argv[1]; plan = json.load(open(os.path.join(b, 'plan.json')))
for f in plan['files']: shutil.copy2(os.path.join(b, 'files', f.lstrip('/')), f)
PY
      [ -e "$B/post-script.before" ] && cp -a "$B/post-script.before" "$POST"   # si no existía se deja: llamadas que ya lo tomaron lo necesitan
      reload_and_count >/dev/null
      echo "⚠️  Si el error fue 'cargado y no esperado': esas líneas son ediciones AJENAS que estaban en disco y quedaron cargadas; revisarlas."
      echo "auto $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$B/rolled_back"
      die "apply revertido. Dialplan restaurado desde $B"
    fi
    echo "✅ APLICADO. Dialplan recargado sin reiniciar Asterisk. Dialplan idéntico salvo las líneas MixMonitor cambiadas: $AFTER_LINE"
    echo "   Backup y plan: $B"
    echo "   Siguiente: esperar llamadas contestadas y correr VALIDATE_WF10_AFTER_DEPLOY.sh. Recién después: $0 finalize"
    ;;

  status)
    echo "== ASTERISK_WF10_FIX: STATUS =="
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    set +e; plan "$T" >/dev/null; set -e
    python3 - "$T" <<'PY'
import json, sys, os
p = json.load(open(os.path.join(sys.argv[1], 'plan.json')))
print(f"contextos con el fix aplicado: {len(p['already'])} | pendientes: {len(p['targets'])} | problemas: {len(p['errors'])}")
for a in p['already']: print(f"  ✓ [{a['context']}] {a['exten']} p{a['priority']}: {a['app']}")
for t in p['targets']: print(f"  ✗ [{t['context']}] {t['exten']} p{t['priority']}: {t['old_app']}")
PY
    [ -x "$POST" ] && info "post-script instalado: $POST" || echo "✗ post-script NO instalado"
    LAST=$(latest_applied)
    if [ -n "$LAST" ]; then
      SINCE=$(cat "$LAST/applied_at")
      info "último apply: $SINCE ($LAST)"
      info "post-proceso desde entonces: encolados=$(awk -v s="$SINCE" '$1>=s && /wf10-mixmon-post/ && /-> enqueue/' "$ENQ_LOG" 2>/dev/null | wc -l) | sin audio=$(awk -v s="$SINCE" '$1>=s && /wf10-mixmon-post/ && /no se encola/' "$ENQ_LOG" 2>/dev/null | wc -l)"
    fi
    info "cola: $(ls -1 "$QUEUE_DIR" 2>/dev/null | wc -l) | done: $(ls -1 /var/spool/wf10/done 2>/dev/null | wc -l)"
    crontab -l 2>/dev/null | grep -n "$CRON_MATCH" || info "cron viejo: no hay línea activa con $CRON_MATCH"
    ;;

  finalize)
    echo "== ASTERISK_WF10_FIX: FINALIZE (retirar el cron viejo) =="
    LAST=$(latest_applied)
    [ -n "$LAST" ] || die "no hay un apply vigente registrado en $BACKUP_ROOT"
    SINCE=$(cat "$LAST/applied_at")
    N=$(awk -v s="$SINCE" '$1>=s && /wf10-mixmon-post/ && /-> enqueue/' "$ENQ_LOG" 2>/dev/null | wc -l)
    MIN="${MIN_ENQUEUED:-3}"
    if [ "$N" -lt "$MIN" ] && [ "$FORCE" != "1" ]; then die "sólo $N grabación(es) encolada(s) por el mecanismo nuevo desde $SINCE (mínimo $MIN). Validá primero con VALIDATE_WF10_AFTER_DEPLOY.sh (FORCE=1 para forzar)."; fi
    crontab -l > "$LAST/crontab.before-finalize" 2>/dev/null || die "no se pudo leer el crontab"
    if ! grep -q "^[^#].*$CRON_MATCH" "$LAST/crontab.before-finalize"; then echo "ℹ️  No hay línea activa con $CRON_MATCH: nada que hacer."; exit 0; fi
    python3 - "$LAST/crontab.before-finalize" "$CRON_MATCH" > "$LAST/crontab.after-finalize" <<'PY'
import sys, datetime
src, match = sys.argv[1], sys.argv[2]
stamp = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
for line in open(src, encoding='utf-8', errors='replace').read().split('\n'):
    if match in line and not line.lstrip().startswith('#'):
        print(f"# WF10-FIX {stamp} desactivado (productor único: MixMonitor post-proceso): {line}")
    else:
        print(line)
PY
    crontab "$LAST/crontab.after-finalize"
    crontab -l | grep -n "$CRON_MATCH"
    touch "$LAST/finalized"
    echo "✅ Cron viejo desactivado (comentado, no borrado). Backup: $LAST/crontab.before-finalize"
    ;;

  rollback)
    B="${2:-$(latest_applied)}"; B="${B%/}"
    [ -f "$B/plan.json" ] && [ -f "$B/applied_at" ] || die "no hay un apply vigente para revertir ('$B')"
    [ -f "$B/rolled_back" ] && die "ese backup ya fue revertido: $B"
    echo "== ASTERISK_WF10_FIX: ROLLBACK desde $B =="
    python3 - "$B" "$FORCE" <<'PY'
import json, os, shutil, sys, hashlib
b, force = sys.argv[1], sys.argv[2] == '1'
plan = json.load(open(os.path.join(b, 'plan.json')))
for f, meta in plan['files'].items():
    cur = hashlib.sha256(open(f, 'rb').read()).hexdigest()
    if meta.get('sha256_after') and cur != meta['sha256_after'] and not force:
        sys.exit(f"{f} fue modificado después del apply (sha distinto). Revisar a mano o FORCE=1")
for f in plan['files']:
    shutil.copy2(os.path.join(b, 'files', f.lstrip('/')), f); print(f"  restaurado {f}")
PY
    # El post-script NO se borra: las llamadas que empezaron con el MixMonitor nuevo lo ejecutan al colgar.
    # Sin referencias en el dialplan es inofensivo; se puede borrar a mano cuando no queden llamadas de antes.
    if [ -e "$B/post-script.before" ]; then cp -a "$B/post-script.before" "$POST"; fi
    [ -e "$B/post-script.absent" ] && info "post-script se deja instalado (sin uso). Para borrarlo cuando no haya llamadas en curso: rm -f $POST"
    if [ "$AST_UP" = "1" ]; then reload_and_count; else echo "⚠️  Asterisk no responde: archivos restaurados; se cargan al iniciar Asterisk."; fi
    if [ -f "$B/finalized" ] && [ -f "$B/crontab.before-finalize" ]; then crontab "$B/crontab.before-finalize"; info "crontab restaurado (cron viejo reactivado)"; fi
    date -u +%Y-%m-%dT%H:%M:%SZ > "$B/rolled_back"
    echo "✅ ROLLBACK completo (sin reiniciar Asterisk)."
    ;;
  *) die "acción desconocida: $ACTION (check|apply|status|finalize|rollback)";;
esac
