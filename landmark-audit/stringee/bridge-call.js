import { chromium } from "playwright-core";
import crypto from "crypto";
import http from "http";
import fs from "fs";

const sid = process.env.PCC_STRINGEE_SID;
const secret = process.env.PCC_STRINGEE_SECRET;

const AGENT_ID =
  process.env.CALL_AGENT_ID ||
  "agent_5701kramx550e3qs2tm11661b48p";

const FROM_NUMBER =
  process.env.CALL_FROM ||
  "917971730907";

const TO_NUMBER =
  process.env.CALL_TO;

const LEAD_ID =
  process.env.CALL_LEAD_ID || "";

const FULL_NAME =
  process.env.CALL_FULL_NAME || "";

const COUNTRY =
  process.env.CALL_COUNTRY || "India";

const LANGUAGE =
  process.env.CALL_LANGUAGE || "hi";

const CALL_ATTEMPTS =
  process.env.CALL_ATTEMPTS || "1";

const INTERNAL_PORT =
  Number(process.env.CALL_INTERNAL_PORT || 18001);

if (!TO_NUMBER) {
  console.error("Missing CALL_TO");
  process.exit(2);
}

if (!sid || !secret) {
  console.error("Missing PCC_STRINGEE_SID / PCC_STRINGEE_SECRET");
  process.exit(1);
}

function b64url(input) {
  return Buffer.from(input)
    .toString("base64")
    .replace(/=/g, "")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
}

function makeClientToken() {
  const now = Math.floor(Date.now() / 1000);

  const header = {
    alg: "HS256",
    typ: "JWT",
    cty: "stringee-api;v=1"
  };

  const payload = {
    jti: `${sid}-${Date.now()}`,
    iss: sid,
    exp: now + 3600,
    userId: `stringee-ai-worker-${Date.now()}`
  };

  const h = b64url(JSON.stringify(header));
  const p = b64url(JSON.stringify(payload));

  const sig = crypto
    .createHmac("sha256", secret)
    .update(`${h}.${p}`)
    .digest();

  return `${h}.${p}.${b64url(sig)}`;
}

const token = makeClientToken();

const html = `
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<script src="https://cdn.stringee.com/sdk/web/latest/stringee-web-sdk.min.js"></script>
</head>
<body>

<audio id="remoteAudio" autoplay></audio>

<script>
const AGENT_ID = ${JSON.stringify(AGENT_ID)};
const BRIDGE_LEAD_ID = ${JSON.stringify(LEAD_ID)};
const BRIDGE_PHONE = ${JSON.stringify(TO_NUMBER)};
const BRIDGE_FULL_NAME = ${JSON.stringify(FULL_NAME)};
const BRIDGE_COUNTRY = ${JSON.stringify(COUNTRY)};
const BRIDGE_LANGUAGE = ${JSON.stringify(LANGUAGE)};
const BRIDGE_CALL_ATTEMPTS = ${JSON.stringify(CALL_ATTEMPTS)};

const STRINGEE_SERVER_ADDRS = [
  "wss://india-s1.stringee.com:32082/",
  "wss://india-s2.stringee.com:32082/",
  "wss://india-s3.stringee.com:32082/"
];

let elevenWs = null;
let outputCtx = null;
let outputDestination = null;
let nextPlaybackTime = 0;

let inputCtx = null;
let inputProcessor = null;
let inputSource = null;

let answered = false;
let bridgeStarted = false;
let elevenReady = false;

let preAnswerAudioQueue = [];
let pendingElevenContext = [];
let lastStringeeState = null;

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";

  for (let i = 0; i < bytes.length; i += 32768) {
    binary += String.fromCharCode(
      ...bytes.subarray(i, Math.min(i + 32768, bytes.length))
    );
  }

  return btoa(binary);
}

function base64ToArrayBuffer(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);

  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }

  return bytes.buffer;
}

function floatToPCM16(samples) {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);

  for (let i = 0; i < samples.length; i++) {
    let s = Math.max(-1, Math.min(1, samples[i]));
    s = s < 0 ? s * 0x8000 : s * 0x7fff;
    view.setInt16(i * 2, s, true);
  }

  return buffer;
}

function pcm16ToFloat32(buffer) {
  const view = new DataView(buffer);
  const out = new Float32Array(buffer.byteLength / 2);

  for (let i = 0; i < out.length; i++) {
    out[i] = view.getInt16(i * 2, true) / 32768;
  }

  return out;
}

function resampleTo16k(input, sourceRate) {
  if (sourceRate === 16000) {
    return new Float32Array(input);
  }

  const ratio = sourceRate / 16000;
  const length = Math.round(input.length / ratio);
  const output = new Float32Array(length);

  for (let i = 0; i < length; i++) {
    const pos = i * ratio;
    const left = Math.floor(pos);
    const right = Math.min(left + 1, input.length - 1);
    const frac = pos - left;

    output[i] =
      input[left] * (1 - frac) +
      input[right] * frac;
  }

  return output;
}

async function prepareVirtualMicrophone() {
  outputCtx = new AudioContext();
  await outputCtx.resume();

  outputDestination = outputCtx.createMediaStreamDestination();
  nextPlaybackTime = outputCtx.currentTime;

  const virtualStream = outputDestination.stream;

  console.log("AI MIC: virtual stream ready");

  const originalGetUserMedia =
    navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);

  navigator.mediaDevices.getUserMedia = async function(constraints) {
    if (constraints && constraints.audio) {
      console.log("AI MIC: Stringee requested microphone");
      return virtualStream;
    }

    return originalGetUserMedia(constraints);
  };
}

function playElevenAudioNow(base64) {
    if (window.__recordAudio) window.__recordAudio('agent', base64);
    if (!outputCtx || !outputDestination) return;

    const pcm = base64ToArrayBuffer(base64);
    const samples = pcm16ToFloat32(pcm);

    const audioBuffer =
      outputCtx.createBuffer(1, samples.length, 16000);

    audioBuffer.getChannelData(0).set(samples);

    const src = outputCtx.createBufferSource();
    src.buffer = audioBuffer;
    src.connect(outputDestination);

    const now = outputCtx.currentTime;

    if (nextPlaybackTime < now) {
      nextPlaybackTime = now;
    }

    src.start(nextPlaybackTime);
    nextPlaybackTime += samples.length / 16000;
  }

  function playElevenAudio(base64) {
    /*
      ElevenLabs existe desde antes de marcar.
      Si todavía no contestaron, guardamos su audio.
    */
    if (!answered) {
      preAnswerAudioQueue.push(base64);
      return;
    }

    playElevenAudioNow(base64);
  }

  function flushPreAnswerAudio() {
    if (!answered) return;

    const queue = preAnswerAudioQueue;
    preAnswerAudioQueue = [];

    console.log(
      "ELEVENLABS: releasing pre-answer audio:",
      queue.length
    );

    for (const chunk of queue) {
      playElevenAudioNow(chunk);
    }
  }

  function sendElevenContext(text) {
    const value = String(text || "").trim();
    if (!value) return;

    if (
      elevenWs &&
      elevenWs.readyState === WebSocket.OPEN &&
      elevenReady
    ) {
      elevenWs.send(JSON.stringify({
        type: "contextual_update",
        text: value
      }));

      console.log("ELEVENLABS CONTEXT:", value);
      return;
    }

    pendingElevenContext.push(value);
  }

  function flushElevenContext() {
    if (
      !elevenWs ||
      elevenWs.readyState !== WebSocket.OPEN ||
      !elevenReady
    ) return;

    const queue = pendingElevenContext;
    pendingElevenContext = [];

    for (const value of queue) {
      elevenWs.send(JSON.stringify({
        type: "contextual_update",
        text: value
      }));

      console.log("ELEVENLABS CONTEXT:", value);
    }
  }

  async function waitForElevenReady(timeoutMs = 12000) {
    const start = Date.now();

    while (
      !elevenReady &&
      Date.now() - start < timeoutMs
    ) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }

    return elevenReady;
  }

  async function captureRemoteStream(stream) {
  if (inputCtx) return;

  inputCtx = new AudioContext();
  await inputCtx.resume();

  console.log("REMOTE sampleRate:", inputCtx.sampleRate);

  inputSource = inputCtx.createMediaStreamSource(stream);
  inputProcessor = inputCtx.createScriptProcessor(4096, 1, 1);

  const silent = inputCtx.createGain();
  silent.gain.value = 0;

  inputSource.connect(inputProcessor);
  inputProcessor.connect(silent);
  silent.connect(inputCtx.destination);

  inputProcessor.onaudioprocess = function(event) {
    if (
      !answered ||
      !elevenReady ||
      !elevenWs ||
      elevenWs.readyState !== WebSocket.OPEN
    ) return;

    const samples = event.inputBuffer.getChannelData(0);
    const resampled = resampleTo16k(samples, inputCtx.sampleRate);
    const pcm = floatToPCM16(resampled);
    const b64 = arrayBufferToBase64(pcm);
    if (window.__recordAudio) window.__recordAudio('human', b64);

    elevenWs.send(JSON.stringify({
      user_audio_chunk: b64
    }));
  };

  console.log("REMOTE CAPTURE: active");
}

function connectElevenLabs() {
  if (bridgeStarted) return;

  bridgeStarted = true;

  const url =
    "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=" +
    encodeURIComponent(AGENT_ID);

  console.log("ELEVENLABS: connecting");

  elevenWs = new WebSocket(url);

  elevenWs.onopen = function() {
    console.log("ELEVENLABS: OPEN");

    elevenWs.send(JSON.stringify({
      type: "conversation_initiation_client_data",
      user_id: BRIDGE_PHONE,
      dynamic_variables: {
          lead_id: BRIDGE_LEAD_ID,
          phone: BRIDGE_PHONE,
          full_name: BRIDGE_FULL_NAME,
          country: BRIDGE_COUNTRY,
          language: BRIDGE_LANGUAGE,
          call_attempts: BRIDGE_CALL_ATTEMPTS,
          provider: "STRINGEE",
          call_status: "INITIATED"
        }
    }));

    console.log(
      "ELEVENLABS: initiation sent with dynamic variables"
    );
  };

  elevenWs.onmessage = function(event) {
    let msg;

    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }

    if (msg.type === "conversation_initiation_metadata") {
      const m =
        msg.conversation_initiation_metadata_event || {};

      console.log(
        "ELEVENLABS conversation:",
        m.conversation_id
      );

      console.log(
        "ELEVENLABS formats:",
        m.user_input_audio_format,
        m.agent_output_audio_format
      );

      elevenReady = true;

        console.log(
          "ELEVENLABS: READY FOR AUDIO"
        );

        flushElevenContext();

        sendElevenContext(
          "CALL_ATTEMPT" +
          " | provider=STRINGEE" +
          " | phone=" + BRIDGE_PHONE +
          " | lead_id=" + BRIDGE_LEAD_ID +
          " | attempt=" + BRIDGE_CALL_ATTEMPTS
        );
    }

    if (msg.type === "ping") {
      const p = msg.ping_event || {};

      setTimeout(() => {
        if (
          elevenWs &&
          elevenWs.readyState === WebSocket.OPEN
        ) {
          elevenWs.send(JSON.stringify({
            type: "pong",
            event_id: p.event_id
          }));
        }
      }, p.ping_ms || 0);
    }

    if (msg.type === "user_transcript") {
      console.log(
        "HUMAN:",
        msg.user_transcription_event?.user_transcript || ""
      );
    }

    if (msg.type === "agent_response") {
      console.log(
        "AGENT:",
        msg.agent_response_event?.agent_response || ""
      );
    }

    if (
      msg.type === "audio" &&
      msg.audio_event?.audio_base_64
    ) {
      playElevenAudio(msg.audio_event.audio_base_64);
    }

    if (msg.type === "error") {
      console.log(
        "ELEVENLABS ERROR:",
        JSON.stringify(msg)
      );
    }

    if (
      ![
        "conversation_initiation_metadata",
        "ping",
        "user_transcript",
        "agent_response",
        "audio",
        "vad_score",
        "interruption",
        "error"
      ].includes(msg.type)
    ) {
      console.log(
        "ELEVENLABS EVENT:",
        JSON.stringify(msg)
      );
    }
  };

  elevenWs.onclose = function(e) {
    elevenReady = false;

    console.log(
      "ELEVENLABS CLOSED:",
      e.code,
      "REASON:",
      e.reason || "(none)"
    );
  };
}

window.startBridge = async function(
  token,
  fromNumber,
  toNumber
) {
  await prepareVirtualMicrophone();

      /*
        NO crear ElevenLabs todavia.
        Solo se conecta cuando Stringee confirma Answered (code 3 / SIP 200).
        Ver signalingstate handler mas abajo.
      */

      return await new Promise((resolve) => {
    const client =
      new StringeeClient(STRINGEE_SERVER_ADDRS);

    let call = null;
    let finished = false;

    const finish = async (reason, success) => {
      if (finished) return;
      finished = true;

      console.log("FINISH:", reason);

        sendElevenContext(
          "STRINGEE_FINAL" +
          " | phone=" + BRIDGE_PHONE +
          " | lead_id=" + BRIDGE_LEAD_ID +
          " | answered=" + String(!!answered) +
          " | result=" + String(reason || "") +
          " | state=" + String(lastStringeeState?.reason || "") +
          " | state_code=" + String(lastStringeeState?.code ?? "") +
          " | sip_code=" + String(lastStringeeState?.sipCode ?? "") +
          " | sip_reason=" + String(lastStringeeState?.sipReason || "")
        );

        /*
          Esperar un poco antes de cerrar ElevenLabs para que
          reciba el último estado.
        */
        await new Promise(resolve => setTimeout(resolve, 1000));

        try { inputProcessor?.disconnect(); } catch {}
      try { inputSource?.disconnect(); } catch {}
      try { await inputCtx?.close(); } catch {}
      try { await outputCtx?.close(); } catch {}
      try { elevenWs?.close(); } catch {}
      try { call?.hangup(function(){}); } catch {}

      resolve({
        ok: success,
        reason,
        answered,
        bridgeStarted
      });
    };

    client.on("connect", function() {
      console.log("STRINGEE CLIENT: connected");
    });

    client.on("authen", function(res) {
      console.log(
        "STRINGEE AUTH:",
        JSON.stringify({
          r: res?.r,
          message: res?.message,
          projectId: res?.projectId
        })
      );

      if (!res || res.r !== 0) {
        finish("stringee-auth-failed", false);
        return;
      }

      call = new StringeeCall(
        client,
        fromNumber,
        toNumber,
        false
      );

      call.on("addlocalstream", function(stream) {
        console.log(
          "STRINGEE LOCAL STREAM:",
          !!stream
        );
      });

      call.on(
        "addremotestream",
        async function(stream) {
          console.log(
            "STRINGEE REMOTE STREAM:",
            !!stream
          );

          const audio =
            document.getElementById("remoteAudio");

          audio.srcObject = stream;

          try {
            await audio.play();
          } catch {}

          await captureRemoteStream(stream);
        }
      );

      call.on("mediastate", function(state) {
        console.log(
          "STRINGEE MEDIA:",
          JSON.stringify(state)
        );
      });

      call.on("signalingstate", function(state) {
          console.log(
            "STRINGEE SIGNAL:",
            JSON.stringify(state)
          );

          lastStringeeState = state || null;

          sendElevenContext(
            "STRINGEE_STATUS" +
            " | phone=" + BRIDGE_PHONE +
            " | lead_id=" + BRIDGE_LEAD_ID +
            " | state=" + String(state?.reason || "") +
            " | state_code=" + String(state?.code ?? "") +
            " | sip_code=" + String(state?.sipCode ?? "") +
            " | sip_reason=" + String(state?.sipReason || "")
          );

          if (state?.code === 3 && !answered) {
              answered = true;

              console.log("PHONE: ANSWERED");

              connectElevenLabs();

              sendElevenContext(
                "PHONE_ANSWERED" +
                " | phone=" + BRIDGE_PHONE +
                " | lead_id=" + BRIDGE_LEAD_ID +
                " | sip_code=" + String(state?.sipCode ?? 200)
              );

              flushPreAnswerAudio();
            }

          if (state?.code === 5 || state?.code === 6) {
          finish(
            "call-ended-" + String(state?.reason || ""),
            answered
          );
        }
      });

      call.on("error", function(info) {
          console.log(
            "STRINGEE CALL ERROR:",
            JSON.stringify(info)
          );

          sendElevenContext(
            "STRINGEE_ERROR" +
            " | phone=" + BRIDGE_PHONE +
            " | lead_id=" + BRIDGE_LEAD_ID +
            " | error=" + JSON.stringify(info || {})
          );
        });

      console.log(
        "CALLING:",
        fromNumber,
        "->",
        toNumber
      );

      call.makeCall(function(res) {
        console.log(
            "MAKECALL:",
            JSON.stringify(res)
          );

          sendElevenContext(
            "STRINGEE_MAKECALL" +
            " | phone=" + BRIDGE_PHONE +
            " | lead_id=" + BRIDGE_LEAD_ID +
            " | stringee_call_id=" + String(res?.callId || "") +
            " | result=" + String(res?.r ?? "") +
            " | message=" + String(res?.message || "")
          );

          if (res && res.r !== undefined && res.r !== 0) {
          finish("makecall-failed", false);
        }
      });
    });

    client.connect(token);

    setTimeout(() => {
      finish(
        "test-timeout",
        answered && bridgeStarted
      );
    }, 90000);
  });
};

</script>
</body>
</html>
`;

const server = http.createServer((req, res) => {
  res.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store"
  });

  res.end(html);
});

await new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(INTERNAL_PORT, "127.0.0.1", resolve);
});

console.log("=========================================");
console.log(" STRINGEE <-> ELEVENLABS BRIDGE TEST ");
console.log("=========================================");
console.log("FROM:", FROM_NUMBER);
console.log("TO:", TO_NUMBER);
console.log("AGENT:", AGENT_ID);

const browser = await chromium.launch({
  headless: true,
  executablePath: chromium.executablePath(),
  args: [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--autoplay-policy=no-user-gesture-required"
  ]
});

const context = await browser.newContext({
  permissions: ["microphone"],
  baseURL: `http://127.0.0.1:${INTERNAL_PORT}`
});

const page = await context.newPage();

// === STRINGEE RECORDING TAP (solo grabacion, no toca logica del bridge) ===
const _recDir = "/opt/stringee-ai-worker/recordings";
try { fs.mkdirSync(_recDir, { recursive: true }); } catch {}
const _humanChunks = [];
const _agentChunks = [];
await page.exposeFunction("__recordAudio", (who, b64) => {
  try {
    const buf = Buffer.from(b64, "base64");
    if (who === "human") _humanChunks.push(buf);
    else if (who === "agent") _agentChunks.push(buf);
  } catch (e) {}
});
// === FIN TAP ===

page.on("console", msg => {
  console.log("[BROWSER]", msg.text());
});

page.on("pageerror", err => {
  console.log("[PAGE ERROR]", err.message);
});

let exitCode = 2;

try {
  await page.goto(
    `http://127.0.0.1:${INTERNAL_PORT}`,
    {
      waitUntil: "networkidle",
      timeout: 20000
    }
  );

  await page.waitForFunction(
    () => typeof window.StringeeClient !== "undefined",
    null,
    { timeout: 15000 }
  );

  const result = await page.evaluate(
    async ({token, fromNumber, toNumber}) => {
      return await window.startBridge(
        token,
        fromNumber,
        toNumber
      );
    },
    {
      token,
      fromNumber: FROM_NUMBER,
      toNumber: TO_NUMBER
    }
  );

  console.log("");
  console.log("===== RESULT =====");
  console.log(JSON.stringify(result, null, 2));

  exitCode = result.ok ? 0 : 2;

} catch (err) {
  console.error("TEST ERROR:", err.message);
}

try { await browser.close(); } catch {}

// === STRINGEE RECORDING TAP: escribir WAV final ===
try {
  if (_humanChunks.length || _agentChunks.length) {
    const human = Buffer.concat(_humanChunks);
    const agent = Buffer.concat(_agentChunks);
    const maxLen = Math.max(human.length, agent.length);
    const humanPadded = Buffer.concat([human, Buffer.alloc(maxLen - human.length)]);
    const agentPadded = Buffer.concat([agent, Buffer.alloc(maxLen - agent.length)]);

    const numSamples = maxLen / 2;
    const stereo = Buffer.alloc(numSamples * 4);
    for (let i = 0; i < numSamples; i++) {
      stereo.writeInt16LE(humanPadded.readInt16LE(i * 2), i * 4);
      stereo.writeInt16LE(agentPadded.readInt16LE(i * 2), i * 4 + 2);
    }

    const sampleRate = 16000;
    const numChannels = 2;
    const bytesPerSample = 2;
    const blockAlign = numChannels * bytesPerSample;
    const byteRate = sampleRate * blockAlign;
    const dataSize = stereo.length;

    const header = Buffer.alloc(44);
    header.write("RIFF", 0);
    header.writeUInt32LE(36 + dataSize, 4);
    header.write("WAVE", 8);
    header.write("fmt ", 12);
    header.writeUInt32LE(16, 16);
    header.writeUInt16LE(1, 20);
    header.writeUInt16LE(numChannels, 22);
    header.writeUInt32LE(sampleRate, 24);
    header.writeUInt32LE(byteRate, 28);
    header.writeUInt16LE(blockAlign, 32);
    header.writeUInt16LE(16, 34);
    header.write("data", 36);
    header.writeUInt32LE(dataSize, 40);

    const wavBuffer = Buffer.concat([header, stereo]);
    const safePhone = String(TO_NUMBER || "unknown").replace(/[^0-9]/g, "");
    const fileName = `stringee-${safePhone}-${Date.now()}.wav`;
    fs.writeFileSync(`${_recDir}/${fileName}`, wavBuffer);
    console.log("RECORDING SAVED:", fileName, "| human:", human.length, "bytes | agent:", agent.length, "bytes");
  } else {
    console.log("RECORDING: no audio chunks captured, skip WAV write");
  }
} catch (e) {
  console.log("RECORDING ERROR:", e.message);
}
// === FIN TAP ===

try { server.close(); } catch {}

process.exit(exitCode);
