/**
 * Captura de microfono -> PCM 16-bit mono a 16 kHz.
 *
 * Se hace en el navegador a proposito: el servidor recibe exactamente el
 * formato que Whisper consume y no gasta un solo ciclo en transcodificar.
 * Con MediaRecorder llegaria Opus/WebM y habria que pasar ffmpeg por cada
 * fragmento, justo sobre el recurso mas escaso de la maquina.
 */

const CHUNK_SAMPLES = 1600; // 100 ms a 16 kHz

class RecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    const targetRate = opts.targetRate || 16000;

    // Normalmente pedimos el AudioContext ya a 16 kHz y el navegador remuestrea
    // con buena calidad, asi que step === 1. Este camino es el respaldo para
    // navegadores que ignoran la tasa solicitada.
    this._step = sampleRate / targetRate;
    this._resample = Math.abs(this._step - 1) > 1e-6;
    this._t = 0;
    this._last = 0;

    this._buf = new Int16Array(CHUNK_SAMPLES);
    this._n = 0;
    this._sumSquares = 0;
    this._count = 0;
    this._closed = false;

    this.port.onmessage = (event) => {
      if (event.data === 'stop') this._closed = true;
    };
  }

  _emit() {
    const pcm = this._buf.slice(0, this._n);
    const rms = this._count ? Math.sqrt(this._sumSquares / this._count) : 0;
    this.port.postMessage({ pcm: pcm.buffer, rms }, [pcm.buffer]);
    this._buf = new Int16Array(CHUNK_SAMPLES);
    this._n = 0;
    this._sumSquares = 0;
    this._count = 0;
  }

  _push(sample) {
    const clamped = Math.max(-1, Math.min(1, sample));
    this._buf[this._n++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    this._sumSquares += clamped * clamped;
    this._count++;
    if (this._n === CHUNK_SAMPLES) this._emit();
  }

  process(inputs) {
    if (this._closed) return false;

    const input = inputs[0];
    if (!input || input.length === 0 || !input[0]) return true;

    // Mezcla a mono por si el dispositivo entrega estereo.
    const frames = input[0].length;
    let mono;
    if (input.length === 1) {
      mono = input[0];
    } else {
      mono = new Float32Array(frames);
      for (let c = 0; c < input.length; c++) {
        const channel = input[c];
        for (let i = 0; i < frames; i++) mono[i] += channel[i] / input.length;
      }
    }

    if (!this._resample) {
      for (let i = 0; i < frames; i++) this._push(mono[i]);
      return true;
    }

    // Interpolacion lineal con continuidad entre bloques: se antepone la
    // ultima muestra del bloque previo para que el indice fraccionario no
    // pierda nada en la frontera.
    const buf = new Float32Array(frames + 1);
    buf[0] = this._last;
    buf.set(mono, 1);
    this._last = mono[frames - 1];

    let t = this._t;
    const limit = buf.length - 1;
    while (t <= limit - 1) {
      const i = t | 0;
      const frac = t - i;
      this._push(buf[i] * (1 - frac) + buf[i + 1] * frac);
      t += this._step;
    }
    // buf[limit] del bloque actual sera buf[0] del siguiente.
    this._t = t - limit;
    return true;
  }
}

registerProcessor('recorder', RecorderProcessor);
