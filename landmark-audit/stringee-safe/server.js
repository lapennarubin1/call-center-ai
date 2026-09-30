import express from "express";
import { spawn } from "child_process";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import readline from "readline";

const app = express();

app.use(express.json({
  limit: "100kb"
}));

const PORT =
  Number(process.env.WORKER_PORT || 8091);

const HOST =
  process.env.WORKER_HOST || "127.0.0.1";

const MAX_CONCURRENT =
  Number(process.env.MAX_CONCURRENT_CALLS || 5);

const NODE_BIN =
  process.execPath;

const BASE_DIR =
  "/opt/stringee-ai-worker";

const BRIDGE_FILE =
  path.join(BASE_DIR, "bridge-call.js");

const LOG_DIR =
  path.join(BASE_DIR, "logs");

const STATE_DIR =
  path.join(BASE_DIR, "state");

fs.mkdirSync(LOG_DIR, {
  recursive: true
});

fs.mkdirSync(STATE_DIR, {
  recursive: true
});

const jobs = new Map();

let nextInternalPort = 18001;


/* ==========================================================================
   UTILITIES
   ========================================================================== */

function now() {
  return new Date().toISOString();
}

function normalizePhone(value) {
  return String(value || "")
    .replace(/\D/g, "");
}

function activeCalls() {
  let count = 0;

  for (const job of jobs.values()) {
    if (
      job.status === "starting" ||
      job.status === "running"
    ) {
      count++;
    }
  }

  return count;
}

function allocatePort() {

  for (let i = 0; i < 999; i++) {

    const port = nextInternalPort++;

    if (nextInternalPort > 18999) {
      nextInternalPort = 18001;
    }

    let used = false;

    for (const job of jobs.values()) {
      if (
        job.internal_port === port &&
        (
          job.status === "starting" ||
          job.status === "running"
        )
      ) {
        used = true;
        break;
      }
    }

    if (!used) {
      return port;
    }
  }

  throw new Error("no_internal_ports_available");
}


function stateFile(jobId) {
  return path.join(
    STATE_DIR,
    `${jobId}.json`
  );
}


function persist(job) {

  try {

    const temp =
      stateFile(job.job_id) + ".tmp";

    fs.writeFileSync(
      temp,
      JSON.stringify(job, null, 2)
    );

    fs.renameSync(
      temp,
      stateFile(job.job_id)
    );

  } catch (err) {

    console.error(
      "STATE PERSIST ERROR:",
      job.job_id,
      err.message
    );
  }
}


function extractSipCode(state) {

  if (!state) return null;

  const possible = [
    state.sip_code,
    state.sipCode,
    state.sip_status,
    state.sipStatus,
    state.responseCode,
    state.statusCode
  ];

  for (const value of possible) {

    const n = Number(value);

    if (
      Number.isInteger(n) &&
      n >= 100 &&
      n <= 699
    ) {
      return n;
    }
  }

  const raw =
    JSON.stringify(state);

  const match =
    raw.match(
      /(?:SIP|sip)[^0-9]{0,10}([1-6][0-9]{2})/
    ) ||
    raw.match(
      /\b([1-6][0-9]{2})\b/
    );

  return match
    ? Number(match[1])
    : null;
}


function extractSipReason(state) {

  return String(
    state?.sip_reason ||
    state?.sipReason ||
    state?.reason ||
    state?.message ||
    ""
  );
}


function mapSipStatus(code) {

  switch (Number(code)) {

    case 100:
      return "INITIATED";

    case 180:
    case 181:
    case 182:
    case 183:
      return "RINGING";

    case 200:
      return "ANSWERED";

    case 408:
      return "NO_ANSWER";

    case 480:
      return "UNAVAILABLE";

    case 486:
      return "BUSY";

    case 600:
    case 603:
      return "REJECTED";

    default:

      if (
        Number(code) >= 400 &&
        Number(code) <= 699
      ) {
        return "FAILED";
      }

      return null;
  }
}


function isTerminalTelephony(status) {

  return [
    "NO_ANSWER",
    "BUSY",
    "REJECTED",
    "UNAVAILABLE",
    "FAILED"
  ].includes(status);
}


function publicJob(job) {

  const copy = {
    ...job
  };

  delete copy.internal_port;
  delete copy.pid;

  return copy;
}


/* ==========================================================================
   CALLBACK
   ========================================================================== */

async function sendCallback(job) {

  const callbackUrl =
    job.callback_url ||
    process.env.N8N_STATUS_CALLBACK_URL ||
    "";

  if (!callbackUrl) {
    return;
  }

  try {

    const headers = {
      "Content-Type":
        "application/json"
    };

    const token =
      process.env.N8N_STATUS_CALLBACK_TOKEN;

    if (token) {
      headers.Authorization =
        `Bearer ${token}`;
    }

    const response =
      await fetch(
        callbackUrl,
        {
          method: "POST",
          headers,
          body:
            JSON.stringify(
              publicJob(job)
            )
        }
      );

    job.callback_status =
      response.status;

    job.callback_sent_at =
      now();

    persist(job);

    console.log(
      "CALLBACK:",
      job.job_id,
      response.status
    );

  } catch (err) {

    job.callback_error =
      err.message;

    persist(job);

    console.error(
      "CALLBACK ERROR:",
      job.job_id,
      err.message
    );
  }
}


/* ==========================================================================
   EVENT ENGINE
   ========================================================================== */


/* ==========================================================================
   PARSER DE EVENTOS DEL BRIDGE ACTUAL
   No requiere modificar bridge-call.js.
   ========================================================================== */

function parseBridgeLine(job, originalLine) {

  let line =
    String(originalLine || "")
      .replace(/^\[BROWSER\]\s*/, "")
      .trim();

  if (!line) {
    return;
  }


  /* ------------------------------------------------------------------------
     MAKECALL
     ------------------------------------------------------------------------ */

  if (line.startsWith("MAKECALL:")) {

    const raw =
      line.slice(
        "MAKECALL:".length
      ).trim();

    try {

      const response =
        JSON.parse(raw);

      applyEvent(
        job,
        {
          type: "makecall",
          ts: now(),
          response,
          stringee_call_id:
            response.callId ||
            response.call_id ||
            response.callID ||
            response.id ||
            null
        }
      );

    } catch (err) {

      console.error(
        "MAKECALL PARSE ERROR:",
        err.message
      );
    }

    return;
  }


  /* ------------------------------------------------------------------------
     STRINGEE SIGNAL
     ------------------------------------------------------------------------ */

  if (line.startsWith("STRINGEE SIGNAL:")) {

    const raw =
      line.slice(
        "STRINGEE SIGNAL:".length
      ).trim();

    try {

      const state =
        JSON.parse(raw);

      applyEvent(
        job,
        {
          type: "signaling",
          ts: now(),
          state
        }
      );

    } catch (err) {

      console.error(
        "SIGNAL PARSE ERROR:",
        err.message
      );
    }

    return;
  }


  /* ------------------------------------------------------------------------
     ANSWERED
     ------------------------------------------------------------------------ */

  if (
    line === "PHONE: ANSWERED" ||
    line.includes("PHONE: ANSWERED")
  ) {

    applyEvent(
      job,
      {
        type: "answered",
        ts: now(),
        answered: true
      }
    );

    return;
  }


  /* ------------------------------------------------------------------------
     ELEVENLABS CONVERSATION ID
     ------------------------------------------------------------------------ */

  if (
    line.startsWith(
      "ELEVENLABS conversation:"
    )
  ) {

    const conversationId =
      line
        .slice(
          "ELEVENLABS conversation:".length
        )
        .trim();

    if (conversationId) {

      applyEvent(
        job,
        {
          type:
            "elevenlabs_conversation",
          ts:
            now(),
          conversation_id:
            conversationId
        }
      );
    }

    return;
  }


  /* ------------------------------------------------------------------------
     ELEVENLABS ERROR
     ------------------------------------------------------------------------ */

  if (
    line.startsWith(
      "ELEVENLABS ERROR:"
    )
  ) {

    const raw =
      line
        .slice(
          "ELEVENLABS ERROR:".length
        )
        .trim();

    let error = raw;

    try {
      error = JSON.parse(raw);
    } catch {}

    applyEvent(
      job,
      {
        type:
          "elevenlabs_error",
        ts:
          now(),
        error
      }
    );

    return;
  }


  /* ------------------------------------------------------------------------
     ELEVENLABS CLOSED
     ------------------------------------------------------------------------ */

  if (
    line.startsWith(
      "ELEVENLABS CLOSED:"
    )
  ) {

    const match =
      line.match(
        /^ELEVENLABS CLOSED:\s*(\d+).*?REASON:\s*(.*)$/i
      );

    applyEvent(
      job,
      {
        type:
          "elevenlabs_closed",
        ts:
          now(),
        code:
          match
            ? Number(match[1])
            : null,
        reason:
          match
            ? String(match[2] || "")
            : ""
      }
    );

    return;
  }


  /* ------------------------------------------------------------------------
     STRINGEE CALL ERROR
     ------------------------------------------------------------------------ */

  if (
    line.startsWith(
      "STRINGEE CALL ERROR:"
    )
  ) {

    const raw =
      line
        .slice(
          "STRINGEE CALL ERROR:".length
        )
        .trim();

    let info = raw;

    try {
      info = JSON.parse(raw);
    } catch {}

    applyEvent(
      job,
      {
        type:
          "call_error",
        ts:
          now(),
        info
      }
    );

    return;
  }


  /* ------------------------------------------------------------------------
     FINISH
     ------------------------------------------------------------------------ */

  if (
    line.startsWith(
      "FINISH:"
    )
  ) {

    const reason =
      line
        .slice(
          "FINISH:".length
        )
        .trim();

    applyEvent(
      job,
      {
        type:
          "finish",
        ts:
          now(),
        reason,
        success:
          !!job.answered,
        answered:
          !!job.answered,
        bridge_started:
          !!job.elevenlabs_conversation_id,
        eleven_ready:
          !!job.elevenlabs_conversation_id
      }
    );

    return;
  }
}


function applyEvent(job, event) {

  if (
    !event ||
    typeof event !== "object"
  ) {
    return;
  }

  job.last_event_at =
    event.ts || now();

  switch (event.type) {

    case "calling":

      job.telephony_status =
        "INITIATED";

      break;


    case "makecall": {

      const r =
        event.response || {};

      job.stringee_call_id =
        event.stringee_call_id ||
        r.callId ||
        r.call_id ||
        r.callID ||
        r.id ||
        job.stringee_call_id ||
        null;

      break;
    }


    case "signaling": {

      const state =
        event.state || {};

      job.stringee_signaling_code =
        state.code ?? null;

      job.stringee_signaling_reason =
        String(
          state.reason ||
          state.message ||
          ""
        );

      const sipCode =
        extractSipCode(state);

      const sipReason =
        extractSipReason(state);

      if (sipCode) {
        job.sip_code =
          sipCode;
      }

      if (sipReason) {
        job.sip_reason =
          sipReason;
      }

      const mapped =
        mapSipStatus(sipCode);

      if (mapped) {

        job.telephony_status =
          mapped;

        if (
          mapped === "RINGING" &&
          !job.ringing_at
        ) {
          job.ringing_at =
            now();
        }

        if (
          mapped === "ANSWERED"
        ) {

          job.answered = true;

          if (!job.answered_at) {
            job.answered_at =
              now();
          }
        }
      }

      /*
       Stringee WebSDK:
       code 3 = answered.
      */

      if (
        Number(state.code) === 3
      ) {

        job.answered = true;
        job.telephony_status =
          "ANSWERED";

        if (!job.answered_at) {
          job.answered_at =
            now();
        }
      }

      break;
    }


    case "answered":

      job.answered = true;

      job.telephony_status =
        "ANSWERED";

      if (!job.answered_at) {
        job.answered_at =
          now();
      }

      break;


    case "elevenlabs_conversation":

      job.elevenlabs_conversation_id =
        event.conversation_id ||
        null;

      job.ai_status =
        "CONNECTED";

      job.elevenlabs_connected_at =
        now();

      break;


    case "elevenlabs_error":

      job.ai_status =
        "ERROR";

      job.ai_error =
        event.error || null;

      break;


    case "elevenlabs_closed":

      if (
        job.ai_status !== "ERROR"
      ) {
        job.ai_status =
          "CLOSED";
      }

      job.elevenlabs_close_code =
        event.code ?? null;

      job.elevenlabs_close_reason =
        event.reason || "";

      break;


    case "call_error":

      job.call_error =
        event.info || {};

      if (!job.answered) {
        job.telephony_status =
          "FAILED";
      }

      break;


    case "finish":

      job.bridge_finish_reason =
        event.reason || "";

      job.bridge_started =
        !!event.bridge_started;

      job.answered =
        !!event.answered ||
        job.answered;

      /*
       IMPORTANT:
       If answered, we DO NOT classify business outcome here.
       WF9 / ElevenLabs remains owner of post-call SUCCESSFUL /
       SCHEDULED / VOICEMAIL etc.
      */

      if (job.answered) {

        job.telephony_status =
          "ANSWERED";

        job.final_status =
          "ANSWERED";

      } else if (
        isTerminalTelephony(
          job.telephony_status
        )
      ) {

        job.final_status =
          job.telephony_status;

      } else if (
        job.telephony_status ===
        "RINGING"
      ) {

        job.telephony_status =
          "NO_ANSWER";

        job.final_status =
          "NO_ANSWER";

      } else {

        job.telephony_status =
          "FAILED";

        job.final_status =
          "FAILED";
      }

      break;
  }

  persist(job);
}


/* ==========================================================================
   RESTORE STATE
   ========================================================================== */

try {

  for (
    const filename
    of fs.readdirSync(STATE_DIR)
  ) {

    if (
      !filename.endsWith(".json")
    ) {
      continue;
    }

    try {

      const data =
        JSON.parse(
          fs.readFileSync(
            path.join(
              STATE_DIR,
              filename
            ),
            "utf8"
          )
        );

      /*
       A process cannot survive worker restart.
      */

      if (
        data.status === "starting" ||
        data.status === "running"
      ) {

        data.status =
          "failed";

        data.final_status =
          data.final_status ||
          "FAILED";

        data.finished_at =
          data.finished_at ||
          now();

        data.error =
          "worker_restarted";

        persist(data);
      }

      jobs.set(
        data.job_id,
        data
      );

    } catch {}
  }

} catch {}


/* ==========================================================================
   HTTP API
   ========================================================================== */

app.get(
  "/health",
  (req, res) => {

    res.json({
      ok: true,
      service:
        "stringee-ai-worker",
      status:
        "running",
      state_engine:
        true,
      active_calls:
        activeCalls(),
      max_concurrent_calls:
        MAX_CONCURRENT
    });
  }
);


app.get(
  "/stringee-ai/health",
  (req, res) => {

    res.json({
      ok: true,
      provider:
        "STRINGEE",
      project_id:
        648702,
      active_calls:
        activeCalls(),
      state_engine:
        true
    });
  }
);


app.get(
  "/calls/:id",
  (req, res) => {

    const job =
      jobs.get(
        req.params.id
      );

    if (!job) {

      return res
        .status(404)
        .json({
          ok: false,
          error:
            "call_not_found"
        });
    }

    res.json(
      publicJob(job)
    );
  }
);


app.post(
  "/call",
  (req, res) => {

    const body =
      req.body || {};

    const phone =
      normalizePhone(
        body.phone
      );

    const leadId =
      String(
        body.lead_id || ""
      ).trim();

    if (!leadId) {

      return res
        .status(400)
        .json({
          ok: false,
          error:
            "missing_lead_id"
        });
    }

    if (
      phone.length < 8 ||
      phone.length > 15
    ) {

      return res
        .status(400)
        .json({
          ok: false,
          error:
            "invalid_phone"
        });
    }

    if (
      activeCalls() >=
      MAX_CONCURRENT
    ) {

      return res
        .status(429)
        .json({
          ok: false,
          error:
            "worker_capacity_reached",
          active_calls:
            activeCalls(),
          max_concurrent_calls:
            MAX_CONCURRENT
        });
    }

    const jobId =
      crypto.randomUUID();

    let internalPort;

    try {

      internalPort =
        allocatePort();

    } catch (err) {

      return res
        .status(503)
        .json({
          ok: false,
          error:
            err.message
        });
    }

    const fromNumber =
      normalizePhone(
        body.from_number ||
        process.env.DEFAULT_FROM_NUMBER ||
        "917971730907"
      );

    const agentId =
      String(
        body.agent_id ||
        process.env.DEFAULT_AGENT_ID ||
        "agent_5701kramx550e3qs2tm11661b48p"
      );

    const logFile =
      path.join(
        LOG_DIR,
        `${jobId}.log`
      );

    const logStream =
      fs.createWriteStream(
        logFile,
        {
          flags: "a"
        }
      );

    const env = {
      ...process.env,

      CALL_TO:
        phone,

      CALL_FROM:
        fromNumber,

      CALL_LEAD_ID:
        leadId,

      CALL_FULL_NAME:
        String(
          body.full_name || ""
        ),

      CALL_COUNTRY:
        String(
          body.country || "India"
        ),

      CALL_LANGUAGE:
        String(
          body.language || "hi"
        ),

      CALL_ATTEMPTS:
        String(
          body.call_attempts || 1
        ),

      CALL_AGENT_ID:
        agentId,

      CALL_INTERNAL_PORT:
        String(internalPort)
    };

    const job = {

      ok:
        true,

      provider:
        "STRINGEE",

      job_id:
        jobId,

      lead_id:
        leadId,

      phone,

      full_name:
        String(
          body.full_name || ""
        ),

      country:
        String(
          body.country || "India"
        ),

      language:
        String(
          body.language || "hi"
        ),

      call_attempts:
        Number(
          body.call_attempts || 1
        ),

      from_number:
        fromNumber,

      agent_id:
        agentId,

      callback_url:
        String(
          body.callback_url || ""
        ),

      status:
        "starting",

      telephony_status:
        "INITIATED",

      final_status:
        null,

      answered:
        false,

      sip_code:
        null,

      sip_reason:
        "",

      stringee_call_id:
        null,

      elevenlabs_conversation_id:
        null,

      ai_status:
        "NOT_STARTED",

      created_at:
        now(),

      started_at:
        null,

      ringing_at:
        null,

      answered_at:
        null,

      finished_at:
        null,

      exit_code:
        null,

      internal_port:
        internalPort
    };

    jobs.set(
      jobId,
      job
    );

    persist(job);

    const child =
      spawn(
        NODE_BIN,
        [BRIDGE_FILE],
        {
          cwd:
            BASE_DIR,
          env,
          stdio: [
            "ignore",
            "pipe",
            "pipe"
          ]
        }
      );

    job.pid =
      child.pid;

    job.status =
      "running";

    job.started_at =
      now();

    persist(job);


    function consumeStream(
      stream,
      prefix = ""
    ) {

      const rl =
        readline.createInterface({
          input:
            stream
        });

      rl.on(
        "line",
        line => {

          logStream.write(
            prefix +
            line +
            "\n"
          );

          /*
           Parsear el formato REAL que ya produce bridge-call.js.
          */
          parseBridgeLine(
            job,
            line
          );

          /*
           Mantener también compatibilidad futura con CALL_EVENT.
          */
          const marker =
            "CALL_EVENT ";

          const index =
            line.indexOf(marker);

          if (index === -1) {
            return;
          }

          const raw =
            line.slice(
              index +
              marker.length
            );

          try {

            const event =
              JSON.parse(raw);

            applyEvent(
              job,
              event
            );

          } catch (err) {

            logStream.write(
              "[STATE PARSE ERROR] " +
              err.message +
              "\n"
            );
          }
        }
      );
    }


    consumeStream(
      child.stdout
    );

    consumeStream(
      child.stderr,
      "[STDERR] "
    );


    child.on(
      "exit",
      async code => {

        job.exit_code =
          code;

        job.finished_at =
          now();

        job.status =
          code === 0
            ? "completed"
            : "failed";

        /*
         If the browser bridge did not produce a terminal
         state, resolve it safely here.
        */

        if (!job.final_status) {

          if (job.answered) {

            job.telephony_status =
              "ANSWERED";

            job.final_status =
              "ANSWERED";

          } else if (
            isTerminalTelephony(
              job.telephony_status
            )
          ) {

            job.final_status =
              job.telephony_status;

          } else if (
            job.telephony_status ===
            "RINGING"
          ) {

            job.telephony_status =
              "NO_ANSWER";

            job.final_status =
              "NO_ANSWER";

          } else {

            job.telephony_status =
              "FAILED";

            job.final_status =
              "FAILED";
          }
        }


        if (
          job.started_at &&
          job.finished_at
        ) {

          job.duration_secs =
            Math.max(
              0,
              Math.round(
                (
                  new Date(
                    job.finished_at
                  ) -
                  new Date(
                    job.started_at
                  )
                ) / 1000
              )
            );
        }


        persist(job);

        try {
          logStream.end();
        } catch {}


        await sendCallback(
          job
        );
      }
    );


    child.on(
      "error",
      async err => {

        job.status =
          "failed";

        job.telephony_status =
          "FAILED";

        job.final_status =
          "FAILED";

        job.error =
          err.message;

        job.finished_at =
          now();

        persist(job);

        try {
          logStream.end();
        } catch {}

        await sendCallback(
          job
        );
      }
    );


    return res
      .status(202)
      .json({
        ok:
          true,

        provider:
          "STRINGEE",

        status:
          "CALL_IN_PROGRESS",

        telephony_status:
          "INITIATED",

        job_id:
          jobId,

        lead_id:
          leadId,

        phone,

        from_number:
          fromNumber
      });
  }
);



app.get(
  "/call-log",
  async (req, res) => {
    try {
      const params = new URLSearchParams({
        version: '2',
        limit: String(req.query.limit || 100),
        sort_by: 'start_time',
        sort_order: 'desc'
      });
      if (req.query.from_start_time) params.set('from_start_time', req.query.from_start_time);
      if (req.query.to_start_time)   params.set('to_start_time', req.query.to_start_time);

      const now_ts = Math.floor(Date.now() / 1000);
      const header = JSON.stringify({ typ: 'JWT', alg: 'HS256', cty: 'stringee-api;v=1' });
      const payload = JSON.stringify({
        jti: `${process.env.PCC_STRINGEE_SID}-${now_ts}`,
        iss: process.env.PCC_STRINGEE_SID,
        exp: now_ts + 300,
        rest_api: true
      });

      const base64url = (str) => Buffer.from(str).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
      const b64h = base64url(header);
      const b64p = base64url(payload);
      const unsigned = `${b64h}.${b64p}`;
      const sig = crypto.createHmac('sha256', process.env.PCC_STRINGEE_SECRET).update(unsigned).digest('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
      const jwt = `${unsigned}.${sig}`;

      const response = await fetch(`https://api.stringee.com/v1/call/log?${params.toString()}`, {
        headers: { 'X-STRINGEE-AUTH': jwt }
      });
      const data = await response.json();
      res.json(data);
    } catch (e) {
      res.status(500).json({ ok: false, error: e.message });
    }
  }
);

app.listen(
  PORT,
  HOST,
  () => {

    console.log(
      `Stringee AI worker listening on http://${HOST}:${PORT}`
    );
  }
);

// GET /call-recording/:call_id — Descargar audio de llamada Stringee
app.get("/call-recording/:call_id", async (req, res) => {
  try {
    const callId = req.params.call_id;
    const now_ts = Math.floor(Date.now() / 1000);
    
    // JWT HS256
    const header = JSON.stringify({ typ: 'JWT', alg: 'HS256', cty: 'stringee-api;v=1' });
    const payload = JSON.stringify({
      jti: `${process.env.PCC_STRINGEE_SID}-${now_ts}`,
      iss: process.env.PCC_STRINGEE_SID,
      exp: now_ts + 300,
      rest_api: true
    });
    const base64url = s => Buffer.from(s).toString('base64').replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
    const unsigned = `${base64url(header)}.${base64url(payload)}`;
    const sig = crypto.createHmac('sha256', process.env.PCC_STRINGEE_SECRET)
      .update(unsigned).digest('base64').replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
    
    const response = await fetch(`https://api.stringee.com/v1/call/${callId}/recording`,
      { headers: { 'X-STRINGEE-AUTH': `${unsigned}.${sig}` } });
    
    if (!response.ok) {
      return res.status(response.status).json({ ok: false, error: `Stringee API ${response.status}` });
    }
    
    // Devolver audio como base64
    const arrayBuffer = await response.arrayBuffer();
    const base64Audio = Buffer.from(arrayBuffer).toString('base64');
    
    res.json({ ok: true, audio_base64: base64Audio, call_id: callId });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});


app.get("/call-audio/:conversation_id", async (req, res) => {
  try {
    const convId = req.params.conversation_id;
    const elevenApiKey = process.env.ELEVENLABS_API_KEY;
    if (!elevenApiKey) {
      return res.status(500).json({ ok: false, error: "ELEVENLABS_API_KEY no configurada en .env" });
    }
    const response = await fetch(
      `https://api.elevenlabs.io/v1/convai/conversations/${convId}/audio`,
      { headers: { "xi-api-key": elevenApiKey } }
    );
    if (!response.ok) {
      return res.status(response.status).json({ ok: false, error: `ElevenLabs API ${response.status}` });
    }
    const arrayBuffer = await response.arrayBuffer();
    const base64Audio = Buffer.from(arrayBuffer).toString("base64");
    res.json({ ok: true, audio_base64: base64Audio, conversation_id: convId });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// === STRINGEE LOCAL RECORDINGS ENDPOINTS ===
import fsRec from "fs";
import pathRec from "path";
const RECORDINGS_DIR = "/opt/stringee-ai-worker/recordings";
const SENT_DIR = "/opt/stringee-ai-worker/recordings/sent";

app.get("/recordings", (req, res) => {
  try {
    if (!fsRec.existsSync(RECORDINGS_DIR)) {
      return res.json({ ok: true, recordings: [] });
    }
    const files = fsRec.readdirSync(RECORDINGS_DIR)
      .filter(f => f.endsWith(".wav"));

    const recordings = files.map(filename => {
      const match = filename.match(/^stringee-(\d+)-(\d+)\.wav$/);
      const phone = match ? match[1] : "unknown";
      const timestampMs = match ? Number(match[2]) : 0;
      const stat = fsRec.statSync(pathRec.join(RECORDINGS_DIR, filename));
      return {
        filename,
        phone,
        timestamp_ms: timestampMs,
        size_bytes: stat.size,
        created_at: new Date(timestampMs || stat.mtimeMs).toISOString()
      };
    });

    res.json({ ok: true, recordings });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.get("/recordings/:filename", (req, res) => {
  try {
    const filename = req.params.filename;
    if (!/^stringee-[0-9]+-[0-9]+\.wav$/.test(filename)) {
      return res.status(400).json({ ok: false, error: "invalid filename" });
    }
    const filePath = pathRec.join(RECORDINGS_DIR, filename);
    if (!fsRec.existsSync(filePath)) {
      return res.status(404).json({ ok: false, error: "not found" });
    }
    const buffer = fsRec.readFileSync(filePath);
    const base64 = buffer.toString("base64");
    res.json({ ok: true, filename, audio_base64: base64 });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post("/recordings/:filename/mark-sent", (req, res) => {
  try {
    const filename = req.params.filename;
    if (!/^stringee-[0-9]+-[0-9]+\.wav$/.test(filename)) {
      return res.status(400).json({ ok: false, error: "invalid filename" });
    }
    if (!fsRec.existsSync(SENT_DIR)) fsRec.mkdirSync(SENT_DIR, { recursive: true });
    const src = pathRec.join(RECORDINGS_DIR, filename);
    const dst = pathRec.join(SENT_DIR, filename);
    if (!fsRec.existsSync(src)) {
      return res.status(404).json({ ok: false, error: "not found" });
    }
    fsRec.renameSync(src, dst);
    res.json({ ok: true, filename, moved_to: "sent" });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});
// === FIN STRINGEE LOCAL RECORDINGS ENDPOINTS ===
