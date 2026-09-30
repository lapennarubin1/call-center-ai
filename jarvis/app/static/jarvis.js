/* ══════════════════════════════════════════════════════════════
   JARVIS — Frontend
   Grabación por voz, visualizador de onda, conversación, gráficos.

   Nota sobre el ciclo de voz: se graba mientras el reactor está
   presionado (push-to-talk). Es deliberado — la detección automática
   de silencio se dispara sola con el ruido de una oficina y termina
   mandando audio basura. Presionar es explícito y no falla.
   ══════════════════════════════════════════════════════════════ */
(() => {
'use strict';

const cfg = window.JARVIS_CONFIG || {};
const $ = id => document.getElementById(id);

const el = {
  reactor: $('reactor'), label: $('reactorLabel'), wave: $('wave'),
  transcript: $('transcript'), input: $('input'), send: $('sendBtn'),
  screen: $('screen'), telemetry: $('telemetry'), status: $('statusText'),
  lang: $('langBadge'), player: $('player'), hint: $('hint'),
  mute: $('muteBtn'), skillsBtn: $('skillsBtn'), reset: $('resetBtn'),
  confirmModal: $('confirmModal'), confirmText: $('confirmText'),
  confirmYes: $('confirmYes'), confirmNo: $('confirmNo'),
  skillsModal: $('skillsModal'), skillsList: $('skillsList'), skillsClose: $('skillsClose'),
};

const state = {
  busy: false, recording: false, muted: false,
  pendingConfirm: null, charts: [], mediaRecorder: null, chunks: [],
  audioCtx: null, analyser: null, waveRAF: null, stream: null,
};

/* ── Estados del reactor ─────────────────────────────────────── */
const LABELS = { idle:'HABLAR', listening:'ESCUCHANDO', thinking:'PROCESANDO',
                 speaking:'RESPONDIENDO', error:'ERROR' };

function setMode(mode){
  el.reactor.className = 'reactor' + (mode !== 'idle' ? ' ' + mode : '');
  el.label.textContent = LABELS[mode] || LABELS.idle;
  const statuses = { idle:'en línea', listening:'escuchando', thinking:'procesando',
                     speaking:'respondiendo', error:'error' };
  el.status.textContent = statuses[mode] || 'en línea';
}

/* ── Mensajes ────────────────────────────────────────────────── */
function addMsg(role, text, opts = {}){
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role + (opts.error ? ' err' : '');
  const roleEl = document.createElement('div');
  roleEl.className = 'msg-role';
  roleEl.textContent = role === 'user' ? 'vos' : 'jarvis';
  const body = document.createElement('div');
  body.className = 'msg-body';
  body.textContent = text;
  wrap.append(roleEl, body);
  if (opts.skills && opts.skills.length){
    const s = document.createElement('div');
    s.className = 'msg-skills';
    s.textContent = '⚡ ' + [...new Set(opts.skills)].join(' · ');
    wrap.appendChild(s);
  }
  el.transcript.appendChild(wrap);
  el.transcript.scrollTop = el.transcript.scrollHeight;
  return wrap;
}

/* ── Pantalla de datos ───────────────────────────────────────── */
function renderScreen(markdown, charts){
  el.screen.innerHTML = '';
  const hasText = markdown && markdown.trim();
  const hasCharts = charts && charts.length;
  if (!hasText && !hasCharts){
    el.screen.innerHTML = '<div class="screen-empty">Los datos y gráficos aparecen acá.</div>';
    return;
  }
  if (hasText){
    const div = document.createElement('div');
    // marked puede no haber cargado (CDN caída) — degradar a texto plano
    // es mejor que dejar la pantalla vacía.
    try { div.innerHTML = window.marked ? marked.parse(markdown) : escapeHtml(markdown); }
    catch(e){ div.textContent = markdown; }
    el.screen.appendChild(div);
  }
  destroyCharts();
  (charts || []).forEach((spec, i) => renderChart(spec, i));
  if (window.innerWidth <= 860 && (hasText || hasCharts)){
    document.querySelector('.hud-right')?.classList.remove('mobile-open');
  }
}

function escapeHtml(s){
  const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

function destroyCharts(){
  state.charts.forEach(c => { try { c.destroy(); } catch(e){} });
  state.charts = [];
}

const PALETTE = ['#38d9f5','#ffb454','#3ddc97','#ff5f6d','#b490ff','#ffe066'];

function renderChart(spec, idx){
  if (!window.Chart) return;
  const card = document.createElement('div');
  card.className = 'chart-card';
  const title = document.createElement('div');
  title.className = 'chart-title';
  title.textContent = spec.titulo || 'Gráfico';
  const box = document.createElement('div');
  box.className = 'chart-box';
  const canvas = document.createElement('canvas');
  canvas.id = 'chart_' + idx + '_' + Date.now();
  box.appendChild(canvas);
  card.append(title, box);
  if (spec.nota){
    const note = document.createElement('div');
    note.className = 'chart-note';
    note.textContent = spec.nota;
    card.appendChild(note);
  }
  el.screen.appendChild(card);

  const labels = spec.datos.map(r => String(r[spec.eje_x]));
  const tipo = { barras:'bar', lineas:'line', area:'line', torta:'doughnut' }[spec.tipo] || 'bar';

  const datasets = spec.series.map((serie, i) => {
    const color = PALETTE[i % PALETTE.length];
    const base = {
      label: serie,
      data: spec.datos.map(r => r[serie]),
      borderColor: color,
      backgroundColor: tipo === 'doughnut'
        ? spec.datos.map((_, j) => PALETTE[j % PALETTE.length])
        : (spec.tipo === 'area' ? color + '33' : color + 'cc'),
      borderWidth: 2,
    };
    if (spec.tipo === 'area') base.fill = true;
    if (tipo === 'line'){ base.tension = .34; base.pointRadius = 2.5; base.pointHoverRadius = 5; }
    if (tipo === 'bar') base.borderRadius = 4;
    return base;
  });

  const grid = { color:'rgba(64,196,255,.08)' };
  const ticks = { color:'#5c7f96', font:{ size:10, family:'monospace' } };

  try {
    state.charts.push(new Chart(canvas.getContext('2d'), {
      type: tipo,
      data: { labels, datasets },
      options: {
        responsive:true, maintainAspectRatio:false,
        interaction:{ intersect:false, mode:'index' },
        plugins:{
          legend:{
            display: datasets.length > 1 || tipo === 'doughnut',
            labels:{ color:'#9fc4dc', font:{ size:10.5 }, boxWidth:11, padding:11 },
          },
          tooltip:{
            backgroundColor:'rgba(6,12,22,.96)', borderColor:'rgba(56,217,245,.35)',
            borderWidth:1, titleColor:'#38d9f5', bodyColor:'#dff3ff', padding:9,
          },
        },
        scales: tipo === 'doughnut' ? {} : {
          x:{ grid, ticks }, y:{ grid, ticks, beginAtZero:true },
        },
      },
    }));
  } catch(e){
    card.appendChild(Object.assign(document.createElement('div'),
      { className:'chart-note', textContent:'No se pudo dibujar el gráfico.' }));
  }
}

/* ── Llamadas al backend ─────────────────────────────────────── */
async function ask(mensaje, confirmado){
  if (state.busy) return;
  state.busy = true;
  el.send.disabled = true;
  setMode('thinking');
  try {
    const res = await fetch('/api/ask', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ mensaje: mensaje || '', confirmado: confirmado || null }),
    });
    if (res.status === 401){ location.href = '/login'; return; }
    handleReply(await res.json());
  } catch(e){
    setMode('error');
    addMsg('jarvis', 'No pude conectarme al servidor. Revisá la conexión.', { error:true });
    setTimeout(() => setMode('idle'), 2200);
  } finally {
    state.busy = false; el.send.disabled = false;
  }
}

async function sendAudio(blob){
  if (state.busy) return;
  state.busy = true; el.send.disabled = true;
  setMode('thinking');
  const fd = new FormData();
  fd.append('audio', blob, 'audio.webm');
  try {
    const res = await fetch('/api/listen', { method:'POST', body: fd });
    if (res.status === 401){ location.href = '/login'; return; }
    const data = await res.json();
    if (data.error && !data.hablado){
      setMode('error');
      addMsg('jarvis', data.error, { error:true });
      setTimeout(() => setMode('idle'), 2200);
      return;
    }
    if (data.transcripcion) addMsg('user', data.transcripcion);
    handleReply(data);
  } catch(e){
    setMode('error');
    addMsg('jarvis', 'No pude enviar el audio.', { error:true });
    setTimeout(() => setMode('idle'), 2200);
  } finally {
    state.busy = false; el.send.disabled = false;
  }
}

function handleReply(data){
  if (data.idioma) el.lang.textContent = data.idioma.toUpperCase();
  addMsg('jarvis', data.hablado || '(sin respuesta)',
         { error: !!data.error, skills: data.skills_usadas });
  renderScreen(data.pantalla, data.graficos);

  if (data.confirmacion){
    state.pendingConfirm = data.confirmacion;
    el.confirmText.textContent = data.confirmacion.pregunta;
    el.confirmModal.hidden = false;
  }
  if (data.hablado && !state.muted && cfg.voiceEnabled){
    speak(data.hablado, data.idioma || 'es');
  } else {
    setMode('idle');
  }
  refreshTelemetry();
}

async function speak(texto, idioma){
  setMode('speaking');
  try {
    const res = await fetch('/api/speak', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ texto, idioma }),
    });
    const type = res.headers.get('content-type') || '';
    if (!type.includes('audio')){
      // El backend devolvió un error de voz en JSON — no es fatal, la
      // respuesta ya está en pantalla. Se sigue sin audio.
      setMode('idle'); return;
    }
    const url = URL.createObjectURL(await res.blob());
    el.player.src = url;
    el.player.onended = () => { setMode('idle'); URL.revokeObjectURL(url); };
    el.player.onerror = () => { setMode('idle'); URL.revokeObjectURL(url); };
    await el.player.play().catch(() => setMode('idle'));
  } catch(e){
    setMode('idle');
  }
}

/* ── Grabación push-to-talk ──────────────────────────────────── */
async function startRecording(){
  if (state.recording || state.busy || !cfg.voiceEnabled) return;
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio:{ echoCancellation:true, noiseSuppression:true, autoGainControl:true },
    });
  } catch(e){
    addMsg('jarvis', 'No pude acceder al micrófono. Revisá los permisos del navegador.',
           { error:true });
    return;
  }
  state.recording = true;
  state.chunks = [];
  setMode('listening');
  startWave(state.stream);

  const mime = ['audio/webm;codecs=opus','audio/webm','audio/mp4']
    .find(m => MediaRecorder.isTypeSupported(m)) || '';
  state.mediaRecorder = new MediaRecorder(state.stream, mime ? { mimeType: mime } : {});
  state.mediaRecorder.ondataavailable = e => { if (e.data.size) state.chunks.push(e.data); };
  state.mediaRecorder.onstop = () => {
    stopWave();
    state.stream.getTracks().forEach(t => t.stop());
    const blob = new Blob(state.chunks, { type: mime || 'audio/webm' });
    // Menos de ~8 KB es casi seguro un toque accidental, no una frase.
    if (blob.size < 8000){ setMode('idle'); return; }
    sendAudio(blob);
  };
  state.mediaRecorder.start();
}

function stopRecording(){
  if (!state.recording) return;
  state.recording = false;
  try { state.mediaRecorder.stop(); } catch(e){ setMode('idle'); }
}

/* ── Visualizador de onda ────────────────────────────────────── */
function startWave(stream){
  try {
    state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = state.audioCtx.createMediaStreamSource(stream);
    state.analyser = state.audioCtx.createAnalyser();
    state.analyser.fftSize = 256;
    src.connect(state.analyser);
    drawWave();
  } catch(e){ /* sin visualizador, la grabación igual funciona */ }
}

function drawWave(){
  const canvas = el.wave, ctx = canvas.getContext('2d');
  const bins = new Uint8Array(state.analyser.frequencyBinCount);
  const cx = canvas.width / 2, cy = canvas.height / 2, base = canvas.width * .28;

  const loop = () => {
    state.waveRAF = requestAnimationFrame(loop);
    state.analyser.getByteFrequencyData(bins);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const bars = 56;
    for (let i = 0; i < bars; i++){
      const v = bins[Math.floor(i * bins.length / bars)] / 255;
      const ang = (i / bars) * Math.PI * 2 - Math.PI / 2;
      const r1 = base, r2 = base + 6 + v * 42;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(ang) * r1, cy + Math.sin(ang) * r1);
      ctx.lineTo(cx + Math.cos(ang) * r2, cy + Math.sin(ang) * r2);
      ctx.strokeStyle = `rgba(56,217,245,${.25 + v * .75})`;
      ctx.lineWidth = 2;
      ctx.stroke();
    }
  };
  loop();
}

function stopWave(){
  if (state.waveRAF) cancelAnimationFrame(state.waveRAF);
  state.waveRAF = null;
  try { state.audioCtx && state.audioCtx.close(); } catch(e){}
  const ctx = el.wave.getContext('2d');
  ctx.clearRect(0, 0, el.wave.width, el.wave.height);
}

/* ── Telemetría ──────────────────────────────────────────────── */
async function refreshTelemetry(){
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    renderTelemetry(await res.json());
  } catch(e){ /* silencioso: el HUD no puede interrumpir la conversación */ }
}

function renderTelemetry(s){
  const parts = [];
  const rows = arr => arr.map(([k, v, cls]) =>
    `<div class="tele-row"><span class="tele-key">${k}</span>` +
    `<span class="tele-val ${cls || ''}">${v}</span></div>`).join('');

  const nombres = { india:'India', mexico:'México', venezuela:'Venezuela', colombia:'Colombia' };
  Object.entries(s.paises || {}).forEach(([pais, d]) => {
    if (d.error){
      parts.push(`<div class="tele-block"><div class="tele-head">${nombres[pais]||pais}</div>
        <div class="tele-err">sin datos</div></div>`);
      return;
    }
    const asrClass = d.asr_pct >= 15 ? 'good' : (d.asr_pct >= 8 ? 'warn' : 'bad');
    parts.push(`<div class="tele-block"><div class="tele-head">${d.pais} · hoy</div>` +
      rows([['llamadas', d.llamadas_totales],
            ['contestadas', d.contestadas],
            ['ASR', d.asr_pct + '%', asrClass],
            ['costo', '$' + d.costo_estimado_usd]]) + `</div>`);
  });

  const c = s.cuentas_hoy;
  if (c && !c.error){
    parts.push(`<div class="tele-block"><div class="tele-head">Cuentas hoy</div>` +
      rows([['total', c.total, c.total > 0 ? 'good' : '']].concat(
        (c.por_pais || []).map(p => [p.pais, p.cuentas]))) + `</div>`);
  }

  const cc = s.call_center;
  if (cc && cc.switches && cc.switches.length){
    const items = cc.switches.map(sw => {
      const cls = sw.estado === 'todo prendido' ? 'on'
                : sw.estado === 'todo apagado' ? 'off' : 'mid';
      const txt = sw.estado === 'todo prendido' ? 'ON'
                : sw.estado === 'todo apagado' ? 'OFF'
                : (sw.workflows_activos + '/' + sw.workflows_totales);
      return `<div class="tele-row"><span class="tele-key">${sw.switch}</span>` +
             `<span class="sw-pill ${cls}">${txt}</span></div>`;
    }).join('');
    parts.push(`<div class="tele-block"><div class="tele-head">Call Center</div>${items}</div>`);
  } else if (cc && cc.error){
    parts.push(`<div class="tele-block"><div class="tele-head">Call Center</div>
      <div class="tele-err">no disponible</div></div>`);
  }

  el.telemetry.innerHTML = parts.join('') ||
    '<div class="tele-loading">sin datos disponibles</div>';
}

/* ── Catálogo de skills ──────────────────────────────────────── */
async function showSkills(){
  el.skillsModal.hidden = false;
  el.skillsList.innerHTML = '<div class="tele-loading">cargando…</div>';
  try {
    const { skills } = await (await fetch('/api/skills')).json();
    el.skillsList.innerHTML = skills.map(s => `
      <div class="skill-item">
        <div class="skill-name">${s.nombre}${
          s.categoria === 'write' ? '<span class="tag">MODIFICA</span>' : ''}</div>
        <div class="skill-desc">${escapeHtml(s.descripcion.split(' Ejemplos de cuándo')[0])}</div>
        ${s.ejemplos.length ? `<div class="skill-ex">"${escapeHtml(s.ejemplos[0])}"</div>` : ''}
      </div>`).join('');
  } catch(e){
    el.skillsList.innerHTML = '<div class="tele-err">no se pudo cargar el catálogo</div>';
  }
}

/* ── Eventos ─────────────────────────────────────────────────── */
function send(){
  const text = el.input.value.trim();
  if (!text) return;
  el.input.value = '';
  addMsg('user', text);
  ask(text);
}

el.send.addEventListener('click', send);
el.input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });

// Push-to-talk: mouse y touch
['mousedown','touchstart'].forEach(ev =>
  el.reactor.addEventListener(ev, e => { e.preventDefault(); startRecording(); },
                              { passive:false }));
['mouseup','mouseleave','touchend','touchcancel'].forEach(ev =>
  el.reactor.addEventListener(ev, e => { e.preventDefault(); stopRecording(); },
                              { passive:false }));

// Barra espaciadora como push-to-talk, salvo escribiendo
document.addEventListener('keydown', e => {
  if (e.code === 'Space' && document.activeElement !== el.input && !e.repeat){
    e.preventDefault(); startRecording();
  }
});
document.addEventListener('keyup', e => {
  if (e.code === 'Space' && document.activeElement !== el.input){
    e.preventDefault(); stopRecording();
  }
});

el.confirmYes.addEventListener('click', () => {
  el.confirmModal.hidden = true;
  const pending = state.pendingConfirm;
  state.pendingConfirm = null;
  if (pending) ask('', pending);
});
el.confirmNo.addEventListener('click', () => {
  el.confirmModal.hidden = true;
  state.pendingConfirm = null;
  addMsg('jarvis', 'Cancelado, no toqué nada.');
  setMode('idle');
});

el.mute.addEventListener('click', () => {
  state.muted = !state.muted;
  el.mute.textContent = state.muted ? '🔇' : '🔊';
  el.mute.classList.toggle('off', state.muted);
  if (state.muted){ try { el.player.pause(); } catch(e){} setMode('idle'); }
});

el.skillsBtn.addEventListener('click', showSkills);
el.skillsClose.addEventListener('click', () => { el.skillsModal.hidden = true; });
[el.skillsModal, el.confirmModal].forEach(m =>
  m.addEventListener('click', e => {
    // Cerrar tocando afuera — pero NO el de confirmación: cerrarlo por
    // accidente y perder el contexto de qué se iba a ejecutar es peor
    // que tener que apretar Cancelar.
    if (e.target === m && m === el.skillsModal) m.hidden = true;
  }));

el.reset.addEventListener('click', async () => {
  await fetch('/api/reset', { method:'POST' });
  el.transcript.innerHTML = '';
  renderScreen('', []);
  addMsg('jarvis', 'Conversación nueva. Lo que aprendí sigue guardado.');
  setMode('idle');
});

if (!cfg.voiceEnabled){
  el.hint.textContent = 'La voz está desactivada · escribí abajo';
  el.label.textContent = 'TEXTO';
}

setMode('idle');
refreshTelemetry();
setInterval(refreshTelemetry, 30000);

})();
