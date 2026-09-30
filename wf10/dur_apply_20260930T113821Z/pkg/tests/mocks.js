// Mundo simulado: LeadStudio (comportamiento confirmado en R3/R7), worker Stringee y ElevenLabs.
// - POST /followups incrementa attempts; nextActionAt fija lead.nextFollowUpAt y crea una tarea (outcome null).
// - PATCH nextFollowUpAt se IGNORA (R3). PATCH status/stage funciona; stage valida el enum (R7).
const STAGES = ['NEW', 'CONTACTED', 'FOLLOW_UP', 'ENGAGED', 'INTERESTED', 'UNRESPONSIVE', 'WON', 'LOST'];
const STATUSES = ['NOT_CONTACTED', 'ATTEMPTING', 'NO_ANSWER', 'CONTACTED', 'CLOSED', 'UNREACHABLE'];
const OUTCOMES = ['CONNECTED', 'NO_ANSWER', 'WRONG_NUMBER', 'CALLBACK', 'VOICEMAIL', 'BUSY'];
const CALLSTATUS = ['ANSWERED', 'NO_ANSWER', 'FAILED', 'BUSY'];

class World {
  constructor() {
    this.leads = new Map(); this.followups = new Map(); this.jobs = new Map(); this.convs = new Map();
    this.calls = []; this.failPost = 0; this.failPatch = 0; this.seq = 1; this.tokens = 0;
  }
  lead(id, o = {}) {
    const l = Object.assign({ id, name: 'Test', phone: '+919800000' + String(this.leads.size).padStart(3, '0'), status: 'ATTEMPTING', stage: 'FOLLOW_UP',
      attempts: 0, nextFollowUpAt: null, lastContactedAt: null, updatedAt: new Date(Date.now() - 3600e3).toISOString(), doNotCall: false }, o);
    this.leads.set(id, l); if (!this.followups.has(id)) this.followups.set(id, []); return l;
  }
  addFollowup(leadId, f) { const e = Object.assign({ id: 'fu_' + (this.seq++), leadId, type: 'CALL', createdAt: new Date().toISOString() }, f); this.followups.get(leadId).push(e); return e; }
  callFus(id) { return (this.followups.get(id) || []).filter(f => f.outcome); }
  async http(req) {
    const { method, url } = req; const body = req.body;
    this.calls.push({ method, url, body: body ? JSON.parse(JSON.stringify(body)) : undefined });
    const R = (statusCode, b) => ({ statusCode, body: b });
    let m;
    if (url === 'http://172.18.0.1:8092/token' && method === 'POST') { this.tokens++; return R(200, { accessToken: 'tok' }); }
    if (url === 'https://lead-studio-9gnl.onrender.com/api/auth/login' && method === 'POST') return R(200, { accessToken: 'tok' });
    if (url === 'https://lead-studio-9gnl.onrender.com/api/leads' && method === 'GET') {
      const qs = req.qs || {};
      const all = [...this.leads.values()].filter(l => (!qs.status || l.status === qs.status) && (!qs.dialCode || String(l.phone).startsWith('+' + qs.dialCode)));
      const off = Number(qs.offset || 0), lim = Number(qs.limit || 50);
      return R(200, { leads: all.slice(off, off + lim).map(l => Object.assign({}, l)), total: all.length });
    }
    if ((m = url.match(/^https:\/\/lead-studio-9gnl\.onrender\.com\/api\/leads\/([^/?]+)$/))) {
      const l = this.leads.get(m[1]); if (!l) return R(404, { error: 'Lead not found' });
      if (method === 'GET') return R(200, { lead: Object.assign({}, l) });
      if (method === 'PATCH') {
        if (this.failPatch > 0) { this.failPatch--; return R(500, { error: 'boom' }); }
        if (body.status !== undefined) { if (!STATUSES.includes(body.status)) return R(400, { error: 'invalid status' }); l.status = body.status; }
        if (body.stage !== undefined) { if (!STAGES.includes(body.stage)) return R(400, { error: 'Invalid stage' }); l.stage = body.stage; }
        // nextFollowUpAt: ignorado (R3)
        l.updatedAt = new Date().toISOString();
        return R(200, { lead: Object.assign({}, l) });
      }
    }
    if ((m = url.match(/^https:\/\/lead-studio-9gnl\.onrender\.com\/api\/leads\/([^/?]+)\/followups$/))) {
      const l = this.leads.get(m[1]); if (!l) return R(404, { error: 'Lead not found' });
      if (method === 'GET') return R(200, { followUps: this.followups.get(l.id).map(f => Object.assign({}, f)) });
      if (method === 'POST') {
        if (this.failPost > 0) { this.failPost--; return R(503, { error: 'Service Unavailable' }); }
        if (!OUTCOMES.includes(body.outcome)) return R(400, { error: 'Unrecognized outcome' });
        if (body.callStatus && !CALLSTATUS.includes(body.callStatus)) return R(400, { error: 'Unrecognized callStatus' });
        const f = this.addFollowup(l.id, { outcome: body.outcome, callStatus: body.callStatus, notes: body.notes, durationSeconds: body.durationSeconds,
          providerCallId: this.dropProviderCallId ? null : (body.providerCallId || null), title: 'Call', summary: null, transcript: null, nextActionAt: body.nextActionAt || null });
        l.attempts += 1; l.lastContactedAt = f.createdAt; l.updatedAt = f.createdAt;
        if ('nextActionAt' in body) {
          l.nextFollowUpAt = body.nextActionAt; // supuesto: null limpia el campo (verificar en producción)
          if (body.nextActionAt) this.addFollowup(l.id, { outcome: null, type: 'TASK', title: 'Follow-up', nextActionAt: body.nextActionAt });
        }
        return R(201, { followUp: f });
      }
    }
    if ((m = url.match(/^http:\/\/172\.18\.0\.1:8091\/calls\/([^/?]+)$/)) && method === 'GET') {
      const j = this.jobs.get(m[1]); return j ? R(200, JSON.parse(JSON.stringify(j))) : R(404, { ok: false, error: 'call_not_found' });
    }
    if ((m = url.match(/^https:\/\/api\.elevenlabs\.io\/v1\/convai\/conversations\/([^/?]+)$/)) && method === 'GET') {
      const c = this.convs.get(decodeURIComponent(m[1])); return c ? R(200, JSON.parse(JSON.stringify(c))) : R(404, { detail: 'not found' });
    }
    if (url.startsWith('https://api.elevenlabs.io/v1/convai/conversations?') && method === 'GET') {
      const list = [...this.convs.values()].map(c => ({ conversation_id: c.conversation_id, agent_id: c.agent_id, status: c.status,
        start_time_unix_secs: c.metadata.start_time_unix_secs, call_duration_secs: c.metadata.call_duration_secs }));
      return R(200, { conversations: list, has_more: false });
    }
    return R(599, { error: 'mock: ruta no simulada ' + method + ' ' + url });
  }
  httpFn() {
    const self = this;
    return async function (req) {
      const r = await self.http(req);
      if (req.returnFullResponse) return r;
      if (r.statusCode >= 400 && !req.ignoreHttpStatusErrors) { const e = new Error('HTTP ' + r.statusCode); e.httpCode = r.statusCode; throw e; }
      return r.body;
    };
  }
}

// Fábricas de fixtures
const iso = ms => new Date(ms).toISOString();
function job(o) {
  const now = Date.now();
  return Object.assign({ ok: true, provider: 'STRINGEE', job_id: o.job_id, lead_id: o.lead_id, phone: '919812345678', full_name: 'Test', country: 'india',
    language: 'hi', call_attempts: 1, status: 'completed', telephony_status: 'NO_ANSWER', final_status: 'NO_ANSWER', answered: false,
    sip_code: null, sip_reason: '', stringee_call_id: 'call-' + o.job_id.slice(0, 8), elevenlabs_conversation_id: null, ai_status: 'NOT_STARTED',
    created_at: iso(now - 120e3), started_at: iso(now - 118e3), ringing_at: iso(now - 115e3), answered_at: null, finished_at: iso(now - 60e3),
    exit_code: 0, callback_url: '' }, o);
}
function conv(o) {
  const start = o.start || Math.floor(Date.now() / 1000) - 300;
  const d = {
    agent_id: 'agent_5701kramx550e3qs2tm11661b48p', conversation_id: o.id, status: o.status || 'done',
    transcript: o.transcript || [],
    metadata: { start_time_unix_secs: start, call_duration_secs: o.dur === undefined ? 60 : o.dur, termination_reason: o.term || 'Call ended by remote party' },
    analysis: { call_successful: o.eval || 'success', transcript_summary: o.summary || 'The customer discussed the trading account.', data_collection_results: o.dc || {} },
    conversation_initiation_client_data: { dynamic_variables: Object.assign({ lead_id: o.lead_id, phone: o.phone || '919812345678', call_attempts: String(o.n || 1) }, o.dyn || {}) }
  };
  if (o.provider === 'STRINGEE') d.conversation_initiation_client_data.dynamic_variables.provider = 'STRINGEE';
  return d;
}
module.exports = { World, job, conv, iso, STAGES };
