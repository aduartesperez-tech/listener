/* LISTENER — captura de microfono, WebSocket y transcripcion en vivo. */

const $ = (id) => document.getElementById(id);

const el = {
  who: $('who'),
  insecure: $('insecure'),
  busy: $('busy'),
  error: $('error'),
  info: $('info'),
  title: $('title'),
  start: $('start'),
  stop: $('stop'),
  dot: $('dot'),
  level: $('level'),
  transcript: $('transcript'),
  meetings: $('meetings'),
  sState: $('s-state'),
  sTime: $('s-time'),
  sSegs: $('s-segs'),
  sQueue: $('s-queue'),
  sRtf: $('s-rtf'),
  sModel: $('s-model'),
};

const state = {
  ws: null,
  ctx: null,
  node: null,
  stream: null,
  meetingId: null,
  segments: 0,
  running: false,
  startedAt: 0,
  timer: null,
  bytesSent: 0,
};

// ---------------------------------------------------------------------------
// Utilidades de UI
// ---------------------------------------------------------------------------

function show(node, html, kind) {
  node.className = 'notice ' + (kind || 'info');
  node.innerHTML = html;
}

function hide(node) {
  node.className = 'notice hidden';
}

function hhmmss(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

function setDot(cls) {
  el.dot.className = 'dot' + (cls ? ' ' + cls : '');
}

function clearTranscript() {
  el.transcript.innerHTML = '';
}

function appendLine(seg) {
  if (!el.transcript.querySelector('.line')) clearTranscript();
  const atBottom =
    el.transcript.scrollHeight - el.transcript.scrollTop - el.transcript.clientHeight < 60;

  const line = document.createElement('div');
  line.className = 'line';

  const ts = document.createElement('span');
  ts.className = 'ts';
  ts.textContent = hhmmss(seg.start);

  const txt = document.createElement('span');
  txt.className = 'txt';
  txt.textContent = seg.text;

  line.append(ts, txt);
  el.transcript.appendChild(line);
  if (atBottom) el.transcript.scrollTop = el.transcript.scrollHeight;
}

// ---------------------------------------------------------------------------
// Estado del servidor y listado
// ---------------------------------------------------------------------------

async function refreshStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    el.who.textContent = data.user;
    el.sModel.textContent = data.asr.live_model;

    if (data.busy && !state.running) {
      const a = data.active || {};
      show(
        el.busy,
        `<b>Servidor ocupado.</b> ${escapeHtml(a.user || 'Alguien')} está grabando
         «${escapeHtml(a.title || '')}» (${hhmmss(a.elapsed_sec || 0)}).
         El servidor admite una reunión a la vez.`,
        'warn'
      );
      el.start.disabled = true;
    } else if (!state.running) {
      hide(el.busy);
      el.start.disabled = false;
    }

    const post = data.post || {};
    if (post.processing_id && !state.running) {
      show(
        el.info,
        `Generando el acta final de la reunión #${post.processing_id}
         (${hhmmss(post.progress_sec)} procesados). Si iniciás una reunión ahora,
         ese trabajo se pausa y se retoma después.`,
        'info'
      );
    } else if (!state.running) {
      hide(el.info);
    }
  } catch (err) {
    /* el servidor puede estar reiniciando; el siguiente ciclo reintenta */
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function statusBadge(m) {
  const map = {
    live: 'live',
    pending_final: 'work',
    processing_final: 'work',
    done: 'done',
    failed: 'failed',
  };
  return `<span class="badge ${map[m.status] || ''}">${escapeHtml(m.status_label)}</span>`;
}

async function refreshMeetings() {
  try {
    const res = await fetch('/api/meetings');
    const rows = await res.json();
    if (!rows.length) {
      el.meetings.innerHTML =
        '<tr><td colspan="5" class="muted">Todavía no hay reuniones grabadas.</td></tr>';
      return;
    }
    el.meetings.innerHTML = rows
      .map((m) => {
        const date = m.started_at ? new Date(m.started_at) : null;
        const when = date
          ? date.toLocaleString('es', {
              day: '2-digit',
              month: '2-digit',
              year: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
            })
          : '—';
        return `<tr>
          <td><a href="/m/${m.id}">${escapeHtml(m.title)}</a>
              <div class="muted">${escapeHtml(m.created_by || '')}</div></td>
          <td class="muted">${when}</td>
          <td class="muted">${hhmmss(m.duration_sec)}</td>
          <td>${statusBadge(m)}</td>
          <td class="rowactions">
            <a class="badge" href="/api/meetings/${m.id}/transcript.txt">.txt</a>
          </td>
        </tr>`;
      })
      .join('');
  } catch (err) {
    el.meetings.innerHTML =
      '<tr><td colspan="5" class="muted">No se pudo cargar el listado.</td></tr>';
  }
}

// ---------------------------------------------------------------------------
// Grabacion
// ---------------------------------------------------------------------------

function wsUrl() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}/ws/live`;
}

async function startMeeting() {
  hide(el.error);
  hide(el.info);
  el.start.disabled = true;

  let stream;
  try {
    // echoCancellation + AGC ayudan cuando el micrófono está lejos, en sala.
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (err) {
    show(
      el.error,
      `<b>No se pudo abrir el micrófono.</b> ${escapeHtml(err.message)}<br>
       Revisá que el navegador tenga permiso y que ninguna otra app lo esté usando.`,
      'err'
    );
    el.start.disabled = false;
    return;
  }

  state.stream = stream;

  // Pedir el contexto ya a 16 kHz: el navegador remuestrea con buena calidad
  // y el worklet no tiene que interpolar nada.
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  let ctx;
  try {
    ctx = new AudioCtx({ sampleRate: 16000, latencyHint: 'interactive' });
  } catch (err) {
    ctx = new AudioCtx({ latencyHint: 'interactive' });
  }
  state.ctx = ctx;
  await ctx.resume();

  try {
    await ctx.audioWorklet.addModule('/static/recorder.worklet.js');
  } catch (err) {
    show(el.error, `<b>No se pudo cargar el capturador de audio.</b> ${escapeHtml(err.message)}`, 'err');
    await teardown();
    el.start.disabled = false;
    return;
  }

  const ws = new WebSocket(wsUrl());
  ws.binaryType = 'arraybuffer';
  state.ws = ws;

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'start', title: el.title.value }));
  };

  ws.onmessage = (event) => handleMessage(JSON.parse(event.data), ctx, stream);

  ws.onerror = () => {
    show(el.error, '<b>Se perdió la conexión con el servidor.</b>', 'err');
  };

  ws.onclose = () => {
    if (state.running) finishUi('conexión cerrada');
  };
}

function handleMessage(msg, ctx, stream) {
  switch (msg.type) {
    case 'busy': {
      const a = msg.active || {};
      show(
        el.busy,
        `<b>${escapeHtml(msg.message)}</b> En curso: «${escapeHtml(a.title || '')}»
         de ${escapeHtml(a.user || '—')}.`,
        'warn'
      );
      teardown();
      el.start.disabled = false;
      break;
    }

    case 'ready':
      state.meetingId = msg.meeting_id;
      state.segments = 0;
      state.running = true;
      state.startedAt = Date.now();
      el.sModel.textContent = msg.model;
      el.sState.textContent = 'grabando';
      el.stop.disabled = false;
      el.start.disabled = true;
      el.title.disabled = true;
      setDot('live');
      clearTranscript();
      hide(el.busy);
      startMic(ctx, stream);
      state.timer = setInterval(() => {
        el.sTime.textContent = hhmmss((Date.now() - state.startedAt) / 1000);
      }, 500);
      break;

    case 'segment':
      state.segments += 1;
      el.sSegs.textContent = state.segments;
      appendLine(msg);
      break;

    case 'status':
      el.sQueue.textContent = msg.queue_depth;
      el.sRtf.textContent = msg.last_rtf != null ? msg.last_rtf.toFixed(2) : '—';
      // RTF > 1 sostenido = la CPU no da; hay que bajar de modelo.
      if (msg.queue_depth >= 3) {
        setDot('warn');
        el.sState.textContent = 'grabando (con retraso)';
      } else if (state.running) {
        setDot('live');
        el.sState.textContent = 'grabando';
      }
      break;

    case 'limit':
      show(el.info, escapeHtml(msg.message), 'warn');
      stopMeeting();
      break;

    case 'closing':
      el.sState.textContent = 'procesando la cola…';
      setDot('warn');
      break;

    case 'ended':
      finishUi(null, msg);
      break;

    case 'error':
      show(el.error, escapeHtml(msg.message), 'err');
      break;
  }
}

function startMic(ctx, stream) {
  const source = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, 'recorder', {
    numberOfInputs: 1,
    numberOfOutputs: 0,
    processorOptions: { targetRate: 16000 },
  });
  state.node = node;

  node.port.onmessage = (event) => {
    const { pcm, rms } = event.data;
    el.level.style.width = Math.min(100, Math.round(rms * 320)) + '%';
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(pcm);
      state.bytesSent += pcm.byteLength;
    }
  };

  source.connect(node);
}

function stopMeeting() {
  el.stop.disabled = true;
  el.sState.textContent = 'cerrando…';
  if (state.node) state.node.port.postMessage('stop');
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'stop' }));
  }
  // El audio se corta ya; el servidor sigue drenando su cola y avisa con 'ended'.
  releaseAudio();
}

function releaseAudio() {
  if (state.stream) {
    state.stream.getTracks().forEach((t) => t.stop());
    state.stream = null;
  }
  if (state.node) {
    try { state.node.disconnect(); } catch (e) { /* ya desconectado */ }
    state.node = null;
  }
  if (state.ctx) {
    state.ctx.close().catch(() => {});
    state.ctx = null;
  }
  el.level.style.width = '0%';
}

async function teardown() {
  releaseAudio();
  if (state.ws) {
    try { state.ws.close(); } catch (e) { /* ya cerrado */ }
    state.ws = null;
  }
}

function finishUi(reason, msg) {
  state.running = false;
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  releaseAudio();
  setDot('');
  el.sState.textContent = reason || 'finalizada';
  el.sQueue.textContent = '0';
  el.stop.disabled = true;
  el.start.disabled = false;
  el.title.disabled = false;

  if (msg && msg.meeting_id) {
    const extra = msg.will_reprocess
      ? ' El servidor está generando ahora el acta final con el modelo grande.'
      : '';
    show(
      el.info,
      `<b>Reunión guardada.</b> ${msg.segments} frases transcritas.${extra}
       <a href="/m/${msg.meeting_id}">Abrir la reunión →</a>`,
      'info'
    );
  }
  refreshMeetings();
}

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

function init() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    el.insecure.className = 'notice err';
    el.start.disabled = true;
  }

  el.start.addEventListener('click', startMeeting);
  el.stop.addEventListener('click', stopMeeting);

  window.addEventListener('beforeunload', (event) => {
    if (state.running) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  refreshStatus();
  refreshMeetings();
  setInterval(refreshStatus, 4000);
  setInterval(() => {
    if (!state.running) refreshMeetings();
  }, 15000);
}

init();
