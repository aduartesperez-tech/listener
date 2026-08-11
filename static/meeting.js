/* LISTENER — vista de detalle de una reunion. */

const $ = (id) => document.getElementById(id);
const meetingId = Number(location.pathname.split('/').pop());

let data = null;
let tab = 'final';
let poller = null;

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function hhmmss(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

function show(node, html, kind) {
  node.className = 'notice ' + (kind || 'info');
  node.innerHTML = html;
}

function render() {
  $('title').textContent = data.title;
  $('status-label').textContent = data.status_label;
  $('m-start').textContent = data.started_at
    ? new Date(data.started_at).toLocaleString('es')
    : '—';
  $('m-dur').textContent = hhmmss(data.duration_sec);
  $('m-user').textContent = data.created_by || '—';
  $('m-live-model').textContent = data.live_model || '—';
  $('m-final-model').textContent = data.final_model || 'pendiente';

  $('dl-txt').href = `/api/meetings/${meetingId}/transcript.txt`;
  $('dl-wav').href = `/api/meetings/${meetingId}/audio.wav`;
  $('dl-wav').style.display = data.has_audio ? '' : 'none';
  $('player').src = data.has_audio ? `/api/meetings/${meetingId}/audio.wav` : '';

  const hasFinal = data.final_segments.length > 0;
  $('tab-final').disabled = !hasFinal;
  if (!hasFinal && tab === 'final') tab = 'live';
  $('tab-final').setAttribute('aria-selected', String(tab === 'final'));
  $('tab-live').setAttribute('aria-selected', String(tab === 'live'));

  if (data.error) {
    show($('error'), `<b>El acta final falló:</b> ${escapeHtml(data.error)}`, 'err');
  }

  const proc = data.processing || {};
  if (data.status === 'processing_final' && proc.processing_id === meetingId) {
    show(
      $('info'),
      `Generando el acta final con ${escapeHtml(data.final_model || 'el modelo grande')} —
       ${hhmmss(proc.progress_sec)} de audio procesados. La página se actualiza sola.`,
      'info'
    );
  } else if (data.status === 'pending_final') {
    show(
      $('info'),
      'En cola para el acta final. El servidor la procesa cuando no haya ninguna reunión en vivo.',
      'info'
    );
  } else {
    $('info').className = 'notice hidden';
  }

  renderBody();
}

function renderBody() {
  const segments = tab === 'final' ? data.final_segments : data.live_segments;
  const body = $('body');

  if (!segments.length) {
    body.innerHTML =
      '<div class="empty">No hay contenido para esta vista todavía.</div>';
    return;
  }

  let lastSpeaker = null;
  body.innerHTML = segments
    .map((s) => {
      const spk =
        s.speaker && s.speaker !== lastSpeaker
          ? `<span class="spk">${escapeHtml(s.speaker)}:</span>`
          : '';
      lastSpeaker = s.speaker || lastSpeaker;
      return `<div class="line" data-t="${s.start_sec}">
        <span class="ts">${hhmmss(s.start_sec)}</span>
        <span class="txt">${spk}${escapeHtml(s.text)}</span>
      </div>`;
    })
    .join('');

  // Click en una línea = saltar a ese punto del audio.
  body.querySelectorAll('.line').forEach((line) => {
    line.style.cursor = 'pointer';
    line.title = 'Saltar a este punto del audio';
    line.addEventListener('click', () => {
      const player = $('player');
      player.currentTime = Number(line.dataset.t) || 0;
      player.play().catch(() => {});
    });
  });
}

async function load() {
  try {
    const res = await fetch(`/api/meetings/${meetingId}`);
    if (!res.ok) {
      show($('error'), '<b>Reunión no encontrada.</b>', 'err');
      return;
    }
    data = await res.json();
    render();
    schedulePoll();
  } catch (err) {
    show($('error'), '<b>No se pudo cargar la reunión.</b>', 'err');
  }
}

function schedulePoll() {
  // Solo se refresca mientras haya trabajo pendiente en el servidor.
  const busy = ['live', 'pending_final', 'processing_final'].includes(data.status);
  if (poller) clearTimeout(poller);
  if (busy) poller = setTimeout(load, 6000);
}

$('tab-final').addEventListener('click', () => {
  tab = 'final';
  render();
});

$('tab-live').addEventListener('click', () => {
  tab = 'live';
  render();
});

$('reprocess').addEventListener('click', async () => {
  if (!confirm('¿Regenerar el acta final desde la grabación?')) return;
  const res = await fetch(`/api/meetings/${meetingId}/reprocess`, { method: 'POST' });
  const body = await res.json();
  if (!res.ok) {
    show($('error'), `<b>No se pudo encolar:</b> ${escapeHtml(body.error)}`, 'err');
    return;
  }
  load();
});

$('delete').addEventListener('click', async () => {
  if (!confirm('Se borra la reunión, su transcripción y la grabación. ¿Seguir?')) return;
  const res = await fetch(`/api/meetings/${meetingId}`, { method: 'DELETE' });
  if (res.ok) {
    location.href = '/';
    return;
  }
  const body = await res.json();
  show($('error'), `<b>No se pudo borrar:</b> ${escapeHtml(body.error)}`, 'err');
});

load();
