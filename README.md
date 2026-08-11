# LISTENER

Transcripción de reuniones en tiempo real, **100 % local**. El audio nunca sale
de la institución: se captura en el navegador, viaja por Tailscale hasta el
servidor propio y se procesa ahí con Whisper. Ninguna nube, ningún tercero.

---

## Cómo funciona

Whisper **no es un modelo de streaming**: trabaja en ventanas de 30 segundos.
El atajo habitual —re-transcribir un buffer deslizante cada 500 ms— quema CPU
repitiendo trabajo y hace que el texto en pantalla parpadee y se reescriba.

Acá se hace distinto: un **VAD** (detector de voz) marca dónde hay habla y
cierra una *frase* cuando el hablante pausa. Cada frase se transcribe **una sola
vez**. La latencia percibida es `VAD_END_MS` + el tiempo de inferencia: entre 1
y 3 segundos, que para una reunión es imperceptible.

```
NAVEGADOR (laptop o teléfono)                    SERVIDOR UBUNTU
┌──────────────────────────────┐                ┌──────────────────────────────────┐
│  getUserMedia()              │                │                                  │
│         │                    │                │  ┌── NIVEL 1: EN VIVO ─────────┐ │
│         ▼                    │                │  │                             │ │
│  AudioContext @ 16 kHz       │                │  │  WebRTC VAD                 │ │
│         │                    │                │  │    → corta por frases       │ │
│         ▼                    │                │  │         │                   │ │
│  AudioWorklet                │   WebSocket    │  │         ▼                   │ │
│    → mono, PCM Int16   ──────┼───────────────▶│  │  faster-whisper `small`     │ │
│                              │  binario, 16k  │  │  int8, 4 hilos              │ │
│  ◀───────────────────────────┼────────────────┼──┼── texto por frase           │ │
│    texto + RTF + cola        │   JSON         │  │                             │ │
└──────────────────────────────┘                │  └─────────────┬───────────────┘ │
                                                │                │                 │
                                                │        WAV completo              │
                                                │         16 kHz mono              │
                                                │                │                 │
                                                │  ┌── NIVEL 2: AL TERMINAR ─────┐ │
                                                │  │  (el servidor está ocioso)   │ │
                                                │  │                             │ │
                                                │  │  large-v3-turbo, beam 5      │ │
                                                │  │  + diarización (opcional)    │ │
                                                │  │         │                   │ │
                                                │  │         ▼                   │ │
                                                │  │   ACTA FINAL                │ │
                                                │  └─────────────────────────────┘ │
                                                │                │                 │
                                                │              SQLite              │
                                                └──────────────────────────────────┘
```

**Los dos niveles son la clave del diseño.** El modelo pequeño da el texto en
vivo; cuando la reunión termina y la máquina queda libre, se reprocesa la
grabación completa con un modelo grande y contexto de toda la sesión. El acta
final sale mucho mejor que el vivo, y el hardware nunca se ahoga.

---

## Requisitos

- Ubuntu Server con Python **3.10–3.13** (ver nota abajo)
- CPU con **AVX2** (cualquier Intel desde Haswell / AMD desde Zen)
- ~2 GB de RAM libres, ~4 GB de disco para los modelos
- Tailscale instalado y en el tailnet
- Salida a internet **la primera vez** para descargar los modelos

Referencia de la máquina para la que se diseñó: Lenovo con i5-6500T
(4 núcleos, Skylake), 8 GB DDR4, SSD de 240 GB. Ahí aguanta **una reunión
simultánea** con `small`.

> **Un solo módulo de RAM = single channel.** La inferencia de Whisper en CPU
> está limitada por ancho de banda de memoria. Poner un segundo módulo para
> activar dual channel es el upgrade más barato y da más que cualquier ajuste de
> software.

---

## Instalación en el servidor

```bash
sudo apt install -y git
git clone <URL-DEL-REPO> /opt/listener
cd /opt/listener
sudo ./deploy/install.sh
```

El script instala dependencias, crea el usuario de sistema `listener` y el
venv, registra el servicio systemd, lo arranca y publica la app por Tailscale.

Seguí la primera descarga de modelos (tarda unos minutos):

```bash
journalctl -u listener -f
```

### Versión de Python

`ctranslate2` —el motor bajo faster-whisper— se distribuye como **wheel
compilado**. Si no hay wheel para la versión de Python del sistema, pip intenta
compilar CTranslate2 desde fuente y eso no termina bien.

**Ubuntu 26.04 trae Python 3.14 por defecto**, que hoy suele quedar fuera de los
wheels publicados. `install.sh` lo detecta: prefiere `python3.12`/`3.11`/`3.13`
si están, e intenta instalar una del archivo de Ubuntu si no.

Si no hay ninguna disponible, la salida más limpia es `uv`, que baja un CPython
propio sin tocar el del sistema:

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
less /tmp/uv-install.sh                 # revisalo antes de ejecutarlo
sh /tmp/uv-install.sh
~/.local/bin/uv python install 3.12
cd /opt/listener
sudo PYTHON_BIN="$(~/.local/bin/uv python find 3.12)" ./deploy/install.sh
```

`PYTHON_BIN` fuerza el intérprete y `install.sh` recrea el venv si la versión
cambió. Para comprobar qué quedó instalado:

```bash
.venv/bin/python -V
.venv/bin/python -c "import faster_whisper, webrtcvad, ctranslate2; print('OK')"
```

### Actualizar

```bash
cd /opt/listener && sudo ./deploy/update.sh
```

`update.sh` se **niega a reiniciar si hay una reunión en curso**.

---

## Acceso

La app escucha **solo en `127.0.0.1`**. Quien expone el servicio es Tailscale:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve status
```

Eso resuelve dos cosas a la vez:

1. **Certificado TLS válido** en `https://<host>.<tailnet>.ts.net`, con
   renovación automática, sin dominio propio y sin abrir un solo puerto al
   internet. Sin HTTPS válido, `getUserMedia()` no funciona y el micrófono
   simplemente no aparece en el navegador.
2. **Identidad del usuario**: Tailscale Serve inyecta las cabeceras
   `Tailscale-User-Login` / `Tailscale-User-Name`, y la app las usa para
   atribuir cada reunión a quien la creó. Sin contraseñas que administrar.

> ⚠️ **Nunca uses `tailscale funnel`.** Eso publicaría la página en el internet
> abierto. `serve` la deja únicamente dentro del tailnet.

### Restringir quién entra (ACLs)

En la [consola de Tailscale](https://login.tailscale.com/admin/acls), limitá el
acceso al grupo que corresponda:

```json
{
  "groups": {
    "group:transcripcion": ["ana@ejemplo.org", "adrian@ejemplo.org"]
  },
  "tagOwners": {
    "tag:listener": ["autogroup:admin"]
  },
  "grants": [
    {
      "src": ["group:transcripcion"],
      "dst": ["tag:listener"],
      "ip": ["tcp:443"]
    }
  ]
}
```

Y etiquetá el servidor: `sudo tailscale up --advertise-tags=tag:listener`

### Si todos están en la red interna

Cada persona necesita Tailscale en su dispositivo. Si son pocas, trivial. Si son
muchas y no todas son técnicas, hay una variante: sacar el certificado con
`tailscale cert` y apuntar ese mismo nombre a la IP de la LAN en el DNS interno.
El TLS valida igual, porque se valida por **nombre**, no por IP.

---

## Configuración

Todo vive en `.env` (partí de `.env.example`). Lo que más importa:

| Variable | Default | Para qué |
|---|---|---|
| `LIVE_MODEL` | `small` | Modelo del vivo. **En español `base` alucina nombres propios y siglas**; `small` es el piso usable para un acta institucional. |
| `FINAL_MODEL` | `large-v3-turbo` | Modelo del acta final. Alternativas si pesa: `medium`, `small`. |
| `VOCAB_PROMPT` | — | Nombres, siglas y términos de la institución. Se pasa como `initial_prompt` y mejora bastante los nombres propios. **Vale la pena dedicarle un rato.** |
| `VAD_END_MS` | `700` | Silencio que cierra una frase. Es lo que define la latencia percibida. |
| `ENABLE_DIARIZATION` | `false` | Etiquetar quién habla. Ver abajo. |
| `MAX_MEETING_HOURS` | `4` | Corte de seguridad. |

Después de editar `.env`: `sudo systemctl restart listener`

---

## Medí antes de confiar en los defaults

Los números de rendimiento dependen de la máquina. Grabá una muestra de habla
**real**, en el idioma y las condiciones de sala de las reuniones:

```bash
arecord -f S16_LE -r 16000 -c 1 -d 60 /tmp/muestra.wav
sudo -u listener /opt/listener/.venv/bin/python bench.py /tmp/muestra.wav \
  --models base,small,medium
```

Interpretación del **RTF** (segundos de CPU por segundo de audio):

| RTF | Veredicto |
|---|---|
| < 0.5 | Tiempo real con margen |
| 0.5 – 0.8 | Funciona, sin holgura |
| 0.8 – 1.0 | Al límite: la cola se irá acumulando |
| > 1.0 | No sirve para vivo (sí para el acta final, que no tiene prisa) |

Y **comparalo con la calidad del texto**, no solo con el RTF. Para un acta
institucional, que los nombres propios salgan bien pesa más que 200 ms de
latencia.

Durante la reunión, la barra de estado muestra el RTF y la profundidad de cola
en vivo. Si la cola pasa de 3 de forma sostenida, la CPU no da: bajá de modelo.

---

## Diarización (quién habla) — opcional

Está **desactivada a propósito**. `pyannote.audio` arrastra PyTorch (~2.5 GB en
disco) y en un i5-6500T la pasada es lenta. Solo corre en el post-proceso,
nunca en vivo — etiquetas de hablante en tiempo real no caben en este hardware.

```bash
sudo -u listener /opt/listener/.venv/bin/pip install pyannote.audio
```

Después, aceptá las condiciones de
[`pyannote/speaker-diarization-3.1`](https://huggingface.co/pyannote/speaker-diarization-3.1)
en HuggingFace, y en `.env`:

```
ENABLE_DIARIZATION=true
HF_TOKEN=hf_...
```

Si falla o el token no está, el acta sale igual, solo sin etiquetas.

---

## Límites conocidos

- **Una reunión a la vez.** 4 núcleos no dan para dos. El segundo intento recibe
  un aviso claro de "ocupado" en lugar de degradar las dos sesiones.
- **El vivo tiene prioridad absoluta.** Si arranca una reunión mientras se
  genera un acta final, ese trabajo se aborta a mitad y se reencola.
- **Solo se captura el micrófono.** Si la reunión es por Zoom o Meet, se graba
  únicamente lo que se oye en la sala. Para capturar también a los participantes
  remotos hace falta audio de sistema (`getDisplayMedia` con audio de pestaña).
- **Un reinicio a media reunión pierde la sesión en curso.** El WAV y las frases
  ya guardadas sobreviven, y la reunión se reencola para el acta final.
- **Sin GPU útil.** La HD 530 podría acelerar el encoder vía OpenVINO con
  `whisper.cpp`, pero eso implicaría cambiar de motor. Queda como experimento.

---

## Estructura

```
app/
  main.py         FastAPI: rutas HTTP y el WebSocket de la sesión en vivo
  session.py      Sesión en vivo + candado de sesión única
  vad.py          Segmentador de voz en streaming (WebRTC VAD)
  asr.py          Los dos motores Whisper + filtro de alucinaciones
  postprocess.py  Worker del acta final (cede la CPU al vivo)
  diarize.py      Diarización opcional
  audio.py        WAV incremental y conversión a float32
  db.py           SQLite
  config.py       .env
static/
  index.html      Grabador + listado
  meeting.html    Detalle de una reunión
  app.js          Captura, WebSocket, UI del vivo
  recorder.worklet.js   Micrófono → PCM Int16 16 kHz mono
deploy/
  install.sh      Instalación en Ubuntu
  update.sh       Despliegue de cambios
  listener.service Unidad systemd endurecida
bench.py          Benchmark de modelos en la máquina real
```

### Detalle de implementación que conviene conocer

El PCM se genera **en el navegador**, no en el servidor. Con `MediaRecorder`
llegaría Opus/WebM y habría que pasar ffmpeg por cada fragmento, justo sobre el
recurso más escaso de la máquina. Con `AudioWorklet` el servidor recibe
exactamente el formato que Whisper consume y no gasta un ciclo en transcodificar.

En el vivo se usa `condition_on_previous_text=False`. Es crítico en modo
troceado: sin eso, un error se propaga a las frases siguientes y Whisper entra
en bucle. En el acta final sí se activa, porque ahí el contexto continuo mejora
la puntuación.

---

## Desarrollo local

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env
python -m uvicorn app.main:app --reload --port 8000
```

Abrí `http://localhost:8000` — `localhost` cuenta como contexto seguro, así que
el micrófono funciona sin certificado.

### Tests

```bash
python -m pytest
```

Cubren la máquina de estados del VAD (preroll, cierre por silencio, corte
forzado, chunks no alineados al frame, correspondencia entre PCM y timestamps) y
el filtro de alucinaciones. No requieren modelos ni GPU: el VAD se sustituye por
un stub determinista, porque lo que se prueba es el segmentador, no el detector.

---

## Solución de problemas

**No aparece el micrófono / el botón está deshabilitado**
No estás en un contexto seguro. Entrá por el nombre `https://…ts.net`, no por la
IP. `localhost` también sirve para pruebas.

**El texto llega muy atrasado y la cola sube**
La CPU no da con ese modelo. Corré `bench.py`, y bajá `LIVE_MODEL` a `base` o
subí `VAD_END_MS` para agrupar frases más largas (menos llamadas, más latencia).

**Aparecen frases raras tipo "Subtítulos por Amara.org"**
Alucinaciones de Whisper en los silencios. `asr.py` ya filtra las más comunes;
si sale una nueva, agregala a `_HALLUCINATION_MARKERS`.

**Los nombres propios salen mal**
Es lo que `VOCAB_PROMPT` resuelve. Poné ahí los nombres y siglas reales.

**El servicio no arranca**
```bash
journalctl -u listener -n 60 --no-pager
```
