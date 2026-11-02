"""Motor de audio: decodificación, remuestreo de alta calidad y mezcla en tiempo real.

Todo el audio se maneja en float32 a la frecuencia nativa de cada dispositivo
(WASAPI), así Windows no vuelve a remuestrear ni a reducir la profundidad de bits.
"""
import threading
import time

import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr
from scipy.ndimage import minimum_filter1d  # aquí y no en el limitador: nunca se carga en mitad de un sonido


# ---------------------------------------------------------------- dispositivos

def _wasapi_index():
    for i, h in enumerate(sd.query_hostapis()):
        if "WASAPI" in h["name"]:
            return i
    return None


def list_devices(kind):
    """Devuelve [(índice, nombre)] de dispositivos WASAPI de salida o entrada."""
    api = _wasapi_index()
    key = "max_output_channels" if kind == "output" else "max_input_channels"
    return [
        (i, d["name"])
        for i, d in enumerate(sd.query_devices())
        if (api is None or d["hostapi"] == api) and d[key] > 0
    ]


def find_device(name, kind):
    for i, n in list_devices(kind):
        if n == name:
            return i
    return None


def default_device(kind):
    api = _wasapi_index()
    if api is None:
        return None
    idx = sd.query_hostapis(api)["default_output_device" if kind == "output" else "default_input_device"]
    return idx if idx >= 0 else None


def find_cable():
    for i, n in list_devices("output"):
        if "CABLE Input" in n:
            return n
    return ""


def device_name(index):
    return sd.query_devices(index)["name"] if index is not None else ""


# ------------------------------------------------------------- decodificación

def load_audio(path):
    """Carga cualquier archivo de audio -> (array float32 (n, canales), frecuencia)."""
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return data, sr
    except Exception:
        pass
    import av  # mp3, m4a, ogg, opus, webm...
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        ctx = stream.codec_context
        channels = 1 if (ctx.channels or 2) == 1 else 2
        rate = ctx.sample_rate or 48000
        resampler = av.AudioResampler(format="flt", layout="mono" if channels == 1 else "stereo", rate=rate)
        chunks = []
        for frame in container.decode(stream):
            for f in resampler.resample(frame):
                chunks.append(f.to_ndarray().reshape(-1, channels))
        for f in resampler.resample(None):
            chunks.append(f.to_ndarray().reshape(-1, channels))
    if not chunks:
        raise ValueError("El archivo no contiene audio")
    return np.concatenate(chunks).astype(np.float32), rate


def conform(data, sr, target_sr, target_ch):
    """Ajusta canales y remuestrea con SoX en calidad máxima (VHQ)."""
    if data.ndim == 1:
        data = data[:, None]
    ch = data.shape[1]
    if ch != target_ch:
        if target_ch == 1:
            data = data.mean(axis=1, keepdims=True)
        elif ch == 1:
            data = np.repeat(data, target_ch, axis=1)
        else:
            data = data[:, :target_ch] if ch > target_ch else np.pad(data, ((0, 0), (0, target_ch - ch)))
    if sr != target_sr:
        data = soxr.resample(data, sr, target_sr, quality="VHQ")
    return np.ascontiguousarray(data, dtype=np.float32)


TARGET_LUFS = -16.0  # sonoridad común para todos los sonidos y voces (como en streaming)
PEAK_CEILING_DB = -1.0  # nunca por encima de -1 dBFS de pico: el limitador no tiene que trabajar


def loudness_gain(data, sr, target=TARGET_LUFS, ceiling_db=PEAK_CEILING_DB):
    """Ganancia lineal para llevar el audio a la sonoridad objetivo (EBU R128 / LUFS)
    sin que el pico supere el techo. Sube lo flojo y baja lo masterizado muy fuerte."""
    import pyloudnorm as pyln
    if data.ndim == 1:
        data = data[:, None]
    peak = float(np.abs(data).max()) if len(data) else 0.0
    if peak < 1e-5:
        return 1.0
    if len(data) >= int(0.5 * sr):
        loud = pyln.Meter(sr).integrated_loudness(data.astype(np.float64))
    else:  # demasiado corto para medir LUFS: aproximación por RMS
        loud = 20 * np.log10(float(np.sqrt((data ** 2).mean())) + 1e-12) - 0.7
    if not np.isfinite(loud):
        return 1.0
    gain = 10 ** ((target - loud) / 20)
    return float(min(gain, 10 ** (ceiling_db / 20) / peak))


def trim(data, sr, start=0.0, end=None, fade=0.008):
    """Recorta [start, end) segundos con un fundido mínimo en los bordes para que no haya 'clic'."""
    a = max(0, int(start * sr))
    b = len(data) if end is None else min(len(data), int(end * sr))
    seg = data[a:b].copy()
    n = min(int(fade * sr), len(seg) // 4)
    if n > 1:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None] if seg.ndim == 2 else np.linspace(0, 1, n)
        if a > 0:
            seg[:n] *= ramp
        if b < len(data):
            seg[-n:] *= ramp[::-1]
    return seg


# --------------------------------------------------------------------- mezcla

class _Voice:
    __slots__ = ("data", "pos", "gain", "tag")

    def __init__(self, data, gain, tag):
        self.data, self.pos, self.gain, self.tag = data, 0, gain, tag


class Output:
    """Un flujo de salida con su propio mezclador y limitador de picos."""

    def __init__(self):
        self.stream = None
        self.sr = 48000
        self.ch = 2
        self.volume = 1.0
        self.enabled = True
        self._voices = []
        self._lock = threading.Lock()
        self._gain = 1.0

    @property
    def active(self):
        return self.stream is not None and self.enabled

    def open(self, index):
        self.close()
        if index is None:
            return
        info = sd.query_devices(index)
        self.sr = int(info["default_samplerate"])
        self.ch = min(2, info["max_output_channels"])
        self._reset_limiter()
        self.stream = sd.OutputStream(
            device=index, samplerate=self.sr, channels=self.ch, dtype="float32",
            latency="low", callback=self._callback,
            extra_settings=sd.WasapiSettings(auto_convert=True),
        )
        self.stream.start()

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        with self._lock:
            self._voices.clear()

    def add(self, data, gain, tag):
        with self._lock:
            self._voices.append(_Voice(data, gain, tag))

    def stop(self, tag=None):
        with self._lock:
            self._voices = [v for v in self._voices if tag is not None and v.tag != tag]

    def is_playing(self, tag):
        with self._lock:
            return any(v.tag == tag for v in self._voices)

    @property
    def busy(self):
        with self._lock:
            return bool(self._voices)

    def _callback(self, outdata, frames, _time, _status):
        outdata.fill(0)
        with self._lock:
            alive = []
            for v in self._voices:
                chunk = v.data[v.pos:v.pos + frames]
                outdata[:len(chunk)] += chunk * v.gain
                v.pos += len(chunk)
                if v.pos < len(v.data):
                    alive.append(v)
            self._voices = alive
        if self.volume != 1.0:
            outdata *= self.volume
        self._limit(outdata, frames)

    def _reset_limiter(self):
        self._L = max(2, int(self.sr * 0.002)) // 2 * 2  # anticipación: 2 ms (par)
        self._H = int(self.sr * 0.030) // 2 * 2  # mantenimiento: 30 ms (par)
        self._delay = np.zeros((self._L, self.ch), np.float32)
        self._need_hist = np.ones(self._H)
        self._gain_hist = np.ones(self._L - 1)
        self._gain = 1.0

    def _limit(self, x, frames, ceiling=0.98):
        """Limitador con anticipación (el audio sale 2 ms retrasado para ver venir los picos).
        Garantiza que nada pasa de 'ceiling' y la ganancia nunca da saltos:
        mínimo de los próximos 2 ms -> se mantiene 30 ms -> sube despacio (200 ms) -> suavizado."""
        if not frames:
            return
        if getattr(self, "_delay", None) is None or self._delay.shape[1] != x.shape[1]:
            self._reset_limiter()
        L, H = self._L, self._H
        ext = np.concatenate([self._delay, x])
        level = np.abs(ext).max(axis=1)
        self._delay = ext[frames:].copy()
        if (level.max() <= ceiling and self._gain >= 1.0
                and self._need_hist.min() >= 1.0 and self._gain_hist.min() >= 1.0):
            x[:] = ext[:frames]  # caso normal: solo el retraso, sin tocar el sonido
            return
        need = np.minimum(1.0, ceiling / np.maximum(level, 1e-9))
        # 1) anticipación: para cada muestra, el mínimo de los próximos L
        ahead = minimum_filter1d(need, size=L + 1, origin=-(L // 2), mode="nearest")[:frames]
        # 2) mantenimiento: el mínimo de los últimos H (también del bloque anterior)
        hist = np.concatenate([self._need_hist, ahead])
        held = minimum_filter1d(hist, size=H + 1, origin=H // 2, mode="nearest")[H:]
        self._need_hist = hist[-H:]
        # 3) recuperación lenta desde la ganancia con la que acabó el bloque anterior
        idx = np.arange(frames)
        rel = 1.0 / (0.200 * self.sr)
        g = np.minimum(np.minimum.accumulate(held - rel * idx) + rel * idx, self._gain + rel * (idx + 1))
        g = np.minimum(g, 1.0)
        self._gain = float(g[-1])
        # 4) suavizado (media de L muestras): sin saltos y sin pasar nunca del techo
        smooth = np.concatenate([self._gain_hist, g])
        cs = np.concatenate([[0.0], np.cumsum(smooth)])
        gain = (cs[L:] - cs[:-L]) / L
        self._gain_hist = smooth[-(L - 1):]
        x[:] = ext[:frames] * gain[:, None].astype(np.float32)


class AudioEngine:
    def __init__(self):
        self.cable = Output()
        self.monitor = Output()
        self._cache = {}
        # Silencio previo (s) en el cable cuando estaba callado: da tiempo a que
        # Discord/el juego abran el micro con la tecla de hablar automática.
        self.preroll = 0.0

    @property
    def outputs(self):
        return [o for o in (self.cable, self.monitor) if o.active]

    def _add(self, out, buf, gain, tag):
        if out is self.cable and self.preroll > 0 and not out.busy:
            buf = np.concatenate([np.zeros((int(out.sr * self.preroll), out.ch), np.float32), buf])
        out.add(buf, gain, tag)

    def play_file(self, path, gain=1.0, tag=None, start=0.0, end=None):
        """start/end en segundos: recorte no destructivo (el archivo original no se toca)."""
        path = str(path)
        data, sr = None, None
        for out in self.outputs:
            key = (path, out.sr, out.ch)
            buf = self._cache.get(key)
            if buf is None:
                if data is None:
                    data, sr = load_audio(path)
                buf = self._cache[key] = conform(data, sr, out.sr, out.ch)
            if start or end:
                buf = trim(buf, out.sr, start, end)
            self._add(out, buf, gain, tag)

    def play_array(self, data, sr, gain=1.0, tag=None):
        for out in self.outputs:
            self._add(out, conform(data, sr, out.sr, out.ch), gain, tag)

    def preview(self, data, sr):
        """Reproduce solo por los auriculares (para probar voces sin que lo oiga nadie)."""
        out = self.monitor
        if out.stream is not None:
            out.stop("preview")
            out.add(conform(data, sr, out.sr, out.ch), 1.0, "preview")

    def cue(self, *freqs):
        """Pitido(s) corto(s) solo por los auriculares (no llegan al juego): uno por frecuencia, seguidos."""
        out = self.monitor
        if out.stream is None:
            return
        n, gap = int(out.sr * 0.07), np.zeros(int(out.sr * 0.03))
        parts = []
        for freq in freqs:
            parts += [np.sin(2 * np.pi * freq * np.arange(n) / out.sr) * np.hanning(n) * 0.25, gap]
        tone = np.concatenate(parts[:-1])
        out.add(np.repeat(tone[:, None], out.ch, axis=1).astype(np.float32), 1.0, "cue")

    def cached(self, path):
        """(audio, frecuencia) ya decodificado de ese archivo, si está en memoria; si no, None."""
        path = str(path)
        for (p, sr, _ch), buf in self._cache.items():
            if p == path:
                return buf, sr
        return None

    def forget(self, path):
        path = str(path)
        self._cache = {k: v for k, v in self._cache.items() if k[0] != path}

    def stop_all(self):
        for out in (self.cable, self.monitor):
            out.stop()

    def is_playing(self, tag):
        return any(o.is_playing(tag) for o in self.outputs)

    def progress(self, tag):
        """Fracción reproducida (0-1) del sonido con esa etiqueta, o None si no suena."""
        for out in (self.cable, self.monitor):
            with out._lock:
                for v in out._voices:
                    if v.tag == tag:
                        return min(1.0, v.pos / max(1, len(v.data)))
        return None

    def wait_until_free(self, tag, timeout=60):
        end = time.monotonic() + timeout
        while self.is_playing(tag) and time.monotonic() < end:
            time.sleep(0.03)


# ------------------------------------------------------------------ grabación

class Recorder:
    """Graba del micrófono real mientras se mantiene pulsada la tecla."""

    def __init__(self):
        self._stream = None
        self._chunks = []
        self._sr = 48000

    @property
    def recording(self):
        return self._stream is not None

    def start(self, index):
        if self._stream is not None:
            return
        if index is None:
            index = default_device("input")
        info = sd.query_devices(index)
        self._sr = int(info["default_samplerate"])
        self._chunks = []
        self._stream = sd.InputStream(
            device=index, samplerate=self._sr, channels=min(2, info["max_input_channels"]),
            dtype="float32", callback=lambda d, *_: self._chunks.append(d.copy()),
            extra_settings=sd.WasapiSettings(auto_convert=True),
        )
        self._stream.start()

    def stop(self, raw=False):
        """Para y devuelve el audio en mono a 16 kHz (lo que espera Whisper),
        o con raw=True el original (audio, frecuencia) para guardarlo con toda la calidad."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        data = np.concatenate(self._chunks) if self._chunks else np.zeros((0, 1), np.float32)
        if raw:
            return data, self._sr
        return conform(data, self._sr, 16000, 1)[:, 0] if len(data) else np.zeros(0, np.float32)
