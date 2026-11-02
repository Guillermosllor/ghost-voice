"""Reconocimiento de voz (Whisper local) y síntesis de voz (SAPI y Edge neural)."""
import asyncio
import io
import json
import os
import re
import tempfile
import threading
from xml.sax.saxutils import escape

import numpy as np
import soundfile as sf

from .audio import load_audio
from .config import MODELS_DIR

STT_MODELS = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]
# modo -> (idioma en el que hablo, idioma que sale)
MODES = {
    "en-en": ("en", "en", "Inglés → Inglés"),
    "es-es": ("es", "es", "Español → Español"),
    "en-es": ("en", "es", "Inglés → Español  (i → e)"),
    "es-en": ("es", "en", "Español → Inglés  (e → i)"),
}
TRANSLATION_MODELS = {
    ("en", "es"): "michaelfeil/ct2fast-opus-mt-en-es",
    ("es", "en"): "michaelfeil/ct2fast-opus-mt-es-en",
}


# ------------------------------------------------------------ voz -> texto

class Transcriber:
    def __init__(self):
        self.model = None
        self.name = None
        self.device = None
        self._lock = threading.Lock()

    def load(self, name):
        from faster_whisper import WhisperModel
        with self._lock:
            if self.name == name and self.model is not None:
                return self.device
            self.model = None
            try:  # GPU NVIDIA si hay librerías CUDA; si no, CPU
                # int8_float16: la mitad de memoria de gráfica -> sigue rápido con un juego abierto
                model = WhisperModel(name, device="cuda", compute_type="int8_float16", download_root=str(MODELS_DIR))
                list(model.transcribe(np.zeros(16000, np.float32), vad_filter=False)[0])
                self.device = "GPU (CUDA)"
            except Exception:
                model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(MODELS_DIR))
                self.device = "CPU"
            self.model, self.name = model, name
            return self.device

    def keep_warm(self):
        """Mini-transcripción para que el juego no desaloje a Whisper de la memoria de la gráfica."""
        if self.model is None or not self._lock.acquire(blocking=False):
            return
        try:
            list(self.model.transcribe(np.zeros(8000, np.float32), beam_size=1, temperature=0.0,
                                       without_timestamps=True, vad_filter=False)[0])
        finally:
            self._lock.release()

    def transcribe(self, audio, language):
        with self._lock:
            if self.model is None:
                raise RuntimeError("El modelo de reconocimiento aún no está cargado")
            # Micro flojito: subir el volumen (hasta x6, más amplificaría solo ruido)
            peak = float(np.abs(audio).max()) if len(audio) else 0.0
            if 0.0 < peak < 0.5:
                audio = audio * min(6.0, 0.5 / peak)
            segments, _info = self.model.transcribe(
                audio,
                language=language,
                beam_size=1,  # búsqueda simple: casi igual de precisa y mucho más rápida
                temperature=0.0,  # sin reintentos (cada reintento repetía todo el trabajo)
                without_timestamps=True,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            # Mejor no decir nada que decir algo inventado: fuera los trozos de poca confianza
            return " ".join(s.text.strip() for s in segments
                            if s.no_speech_prob < 0.6 and s.avg_logprob > -0.9).strip()


# ------------------------------------------------------------ traducción

class Translator:
    """Traducción local inglés <-> español (Opus-MT en CTranslate2). Sin internet ni límites."""

    def __init__(self):
        self._models = {}
        self._lock = threading.Lock()

    def _get(self, pair):
        if pair not in self._models:
            import ctranslate2
            import sentencepiece
            from huggingface_hub import snapshot_download
            path = snapshot_download(TRANSLATION_MODELS[pair], cache_dir=str(MODELS_DIR))
            try:
                model = ctranslate2.Translator(path, device="cuda", compute_type="float16")
                model.translate_batch([["▁hi", "</s>"]])
            except Exception:
                model = ctranslate2.Translator(path, device="cpu", compute_type="int8")
            src = sentencepiece.SentencePieceProcessor(model_file=os.path.join(path, "source.spm"))
            tgt = sentencepiece.SentencePieceProcessor(model_file=os.path.join(path, "target.spm"))
            self._models[pair] = (model, src, tgt)
        return self._models[pair]

    def preload(self, source, target):
        if source != target:
            with self._lock:
                self._get((source, target))

    def translate(self, text, source, target):
        if source == target or not text:
            return text
        with self._lock:
            model, src, tgt = self._get((source, target))
            sentences = [s for s in re.split(r"(?<=[.!?¿¡])\s+", text) if s.strip()]
            batch = [src.encode(s, out_type=str) + ["</s>"] for s in sentences]
            results = model.translate_batch(batch, beam_size=5)
            return " ".join(tgt.decode(r.hypotheses[0]) for r in results).strip()


# ------------------------------------------------------------ texto -> voz

SAFT48kHz16BitMono = 38
SSFMCreateForWrite = 3
SVSFIsXML = 8


def _com():
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    return win32com.client


def sapi_voices():
    client = _com()
    voice = client.Dispatch("SAPI.SpVoice")
    tokens = voice.GetVoices()
    return [tokens.Item(i).GetDescription() for i in range(tokens.Count)]


def sapi_synth(text, voice_name, rate, pitch):
    """Voz clásica de Windows (estilo Loquendo). Si tienes voces Loquendo SAPI5 instaladas, aparecen aquí."""
    client = _com()
    voice = client.Dispatch("SAPI.SpVoice")
    tokens = voice.GetVoices()
    for i in range(tokens.Count):
        if tokens.Item(i).GetDescription() == voice_name:
            voice.Voice = tokens.Item(i)
            break
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        stream = client.Dispatch("SAPI.SpFileStream")
        fmt = client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = SAFT48kHz16BitMono
        stream.Format = fmt
        stream.Open(path, SSFMCreateForWrite)
        voice.AudioOutputStream = stream
        voice.Rate = int(rate)
        voice.Volume = 100
        voice.Speak(f'<pitch absmiddle="{int(pitch)}">{escape(text)}</pitch>', SVSFIsXML)
        stream.Close()
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        return data, sr
    finally:
        os.remove(path)


def winrt_voices():
    from winrt.windows.media.speechsynthesis import SpeechSynthesizer
    return [f"{v.display_name} ({v.language})" for v in SpeechSynthesizer.all_voices]


def winrt_synth(text, voice_name, rate, pitch):
    """Voces OneCore de Windows (Pablo, Laura… y las que añadas en Configuración > Voz)."""
    from winrt.windows.media.speechsynthesis import SpeechSynthesizer
    from winrt.windows.storage.streams import DataReader

    async def run():
        synth = SpeechSynthesizer()
        for v in SpeechSynthesizer.all_voices:
            if f"{v.display_name} ({v.language})" == voice_name:
                synth.voice = v
        synth.options.speaking_rate = 2 ** (int(rate) / 10)
        synth.options.audio_pitch = 1 + int(pitch) / 10
        stream = await synth.synthesize_text_to_stream_async(text)
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(stream.size)
        buf = bytearray(stream.size)
        reader.read_bytes(buf)
        return bytes(buf)

    wav = asyncio.run(run())
    data, sr = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    return data, sr


PIPER_REPO = "rhasspy/piper-voices"
_QUALITY = {"x_low": 0, "low": 1, "medium": 2, "high": 3}
_piper_catalog = {}  # nombre visible -> (clave del modelo, id de hablante)
_piper_loaded = {}


def piper_voices():
    """Catálogo de voces Piper en inglés y español (la mejor calidad de cada una)."""
    from huggingface_hub import hf_hub_download
    path = hf_hub_download(PIPER_REPO, "voices.json", cache_dir=str(MODELS_DIR))
    with open(path, encoding="utf-8") as f:
        catalog = json.load(f)
    best = {}
    for key, v in catalog.items():
        if v["language"]["code"][:2] not in ("en", "es"):
            continue
        base = key.rsplit("-", 1)[0]
        if base not in best or _QUALITY[v["quality"]] > _QUALITY[catalog[best[base]]["quality"]]:
            best[base] = key
    _piper_catalog.clear()
    for base, key in sorted(best.items()):
        v = catalog[key]
        speakers = v.get("speaker_id_map") or {}
        if v["num_speakers"] <= 1:
            _piper_catalog[key] = (v, None)
        elif v["num_speakers"] <= 24:  # los de cientos de hablantes llenarían la lista
            for name, sid in sorted(speakers.items(), key=lambda kv: kv[1]):
                _piper_catalog[f"{key} #{name}"] = (v, sid)
    return list(_piper_catalog)


def piper_synth(text, voice_name, rate, pitch):
    """Voces neuronales Piper: offline y muy naturales. Se descargan la primera vez que se usan."""
    from huggingface_hub import hf_hub_download
    from piper import PiperVoice, SynthesisConfig
    if not _piper_catalog:
        piper_voices()
    info, speaker = _piper_catalog[voice_name]
    files = sorted(info["files"])
    if voice_name not in _piper_loaded:
        onnx = next(f for f in files if f.endswith(".onnx"))
        local = [hf_hub_download(PIPER_REPO, f, cache_dir=str(MODELS_DIR)) for f in (onnx, onnx + ".json")]
        _piper_loaded[voice_name] = PiperVoice.load(local[0], config_path=local[1])
    voice = _piper_loaded[voice_name]
    # Tono: se genera más lento y se reproduce más rápido -> sube el tono, misma duración.
    shift = 2 ** (int(pitch) / 20)
    cfg = SynthesisConfig(speaker_id=speaker, length_scale=2 ** (-int(rate) / 10) * shift)
    chunks = [c.audio_float_array for c in voice.synthesize(text, syn_config=cfg)]
    if not chunks:
        raise RuntimeError("Piper no generó audio")
    return np.concatenate(chunks).astype(np.float32)[:, None], int(voice.config.sample_rate * shift)


def edge_voices():
    import edge_tts
    voices = asyncio.run(edge_tts.list_voices())
    return sorted(
        (v["ShortName"] for v in voices if v["Locale"].startswith(("es-", "en-"))),
        key=lambda n: (not n.startswith("en-"), n),
    )


def edge_synth(text, voice_name, rate, pitch):
    """Voces neuronales de Microsoft Edge (necesita internet, suenan muy naturales)."""
    import edge_tts

    async def run():
        com = edge_tts.Communicate(text, voice_name, rate=f"{int(rate) * 10:+d}%", pitch=f"{int(pitch) * 5:+d}Hz")
        audio = bytearray()
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        return bytes(audio)

    mp3 = asyncio.run(run())
    fd, path = tempfile.mkstemp(suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(mp3)
    try:
        return load_audio(path)
    finally:
        os.remove(path)


# --------------------------------------------- eSpeak NG (síntesis por formantes)

# Variante propia: sintetizador Klatt original (la familia de DECtalk / Stephen Hawking),
# grave, casi monótona y sin "flutter" (el temblor que hace la voz más humana).
HAWKING_VARIANT = """language variant
name Hawking
gender male
klatt 1
pitch 96 126
flutter 0
voicing 105
consonants 100 90
"""
_espeak = {}
_espeak_lock = threading.Lock()


def _espeak_init():
    if _espeak:
        return _espeak
    import ctypes
    import shutil
    import espeakng_loader
    from .config import DATA_DIR
    data = DATA_DIR / "espeak-ng-data"
    if not data.exists():  # copia propia para poder añadir la variante Hawking
        shutil.copytree(espeakng_loader.get_data_path(), data)
    (data / "voices" / "!v" / "hawking").write_text(HAWKING_VARIANT, encoding="utf-8")
    lib = ctypes.CDLL(str(espeakng_loader.get_library_path()))
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
    chunks = []

    def on_audio(wav, n, _events):
        if wav and n > 0:
            chunks.append(np.ctypeslib.as_array(wav, (n,)).copy())
        return 0

    cb = callback_type(on_audio)
    sr = lib.espeak_Initialize(2, 0, str(DATA_DIR).encode(), 0)  # 2 = AUDIO_OUTPUT_SYNCHRONOUS
    if sr <= 0:
        raise RuntimeError("No se pudo iniciar eSpeak NG")
    lib.espeak_SetSynthCallback(cb)
    lib.espeak_Synth.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                                 ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    _espeak.update(lib=lib, cb=cb, chunks=chunks, sr=sr, variants=sorted(os.listdir(data / "voices" / "!v")))
    return _espeak


_ESPEAK_FIRST = ["hawking", "klatt", "klatt2", "klatt3", "klatt4", "klatt5", "klatt6", "UniRobot", "anikaRobot",
                 "robosoft", "robosoft2", "robosoft3", "robosoft4", "robosoft5", "robosoft6", "robosoft7",
                 "robosoft8", "Demonic", "croak", "whisper"]


def espeak_voices():
    with _espeak_lock:
        variants = _espeak_init()["variants"]
    ordered = [v for v in _ESPEAK_FIRST if v in variants] + [v for v in variants if v not in _ESPEAK_FIRST]
    return [f"{lang}+{v}" for lang in ("en-us", "es") for v in ordered]


def espeak_synth(text, voice_name, rate, pitch):
    """Voces sintetizadas por formantes: robots de verdad y la voz tipo Stephen Hawking."""
    with _espeak_lock:
        e = _espeak_init()
        lib = e["lib"]
        hawking = voice_name.endswith("+hawking")
        lib.espeak_SetVoiceByName(voice_name.encode())
        lib.espeak_SetParameter(1, int((150 if hawking else 175) * 2 ** (int(rate) / 10)), 0)  # palabras/min
        lib.espeak_SetParameter(3, max(0, min(99, 50 + int(pitch) * 5)), 0)  # tono
        lib.espeak_SetParameter(4, 28 if hawking else 50, 0)  # rango de entonación
        e["chunks"].clear()
        raw = text.encode("utf-8")
        lib.espeak_Synth(raw, len(raw) + 1, 0, 0, 0, 1, None, None)  # 1 = UTF-8
        lib.espeak_Synchronize()
        if not e["chunks"]:
            raise RuntimeError("eSpeak no generó audio")
        data = np.concatenate(e["chunks"]).astype(np.float32) / 32768.0
        data *= 0.9 / (float(np.abs(data).max()) or 1.0)  # eSpeak sale flojo: igualar con el resto
        return data[:, None], e["sr"]


# ------------------------------------------------ Kokoro (neuronal, mezclable)

KOKORO_DIR = MODELS_DIR / "kokoro"
_KOKORO_ACCENT = {"a": "EE.UU.", "b": "Reino Unido", "e": "España", "f": "Francia", "h": "India", "i": "Italia",
                  "j": "Japón", "p": "Brasil", "z": "China"}
_kokoro = {}
_kokoro_lock = threading.Lock()
kokoro_blends = {}  # nombre -> [(voz, peso)], lo rellena la interfaz desde la configuración


KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"


def _kokoro_fetch(name):
    """Descarga el modelo la primera vez (~350 MB en total), a un .part para no dejar archivos a medias."""
    path = KOKORO_DIR / name
    if not path.exists():
        import requests
        KOKORO_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        with requests.get(KOKORO_URL + name, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(path)
    return str(path)


def _kokoro_model():
    if "model" not in _kokoro:
        from kokoro_onnx import Kokoro
        _kokoro["model"] = Kokoro(_kokoro_fetch("kokoro-v1.0.onnx"), _kokoro_fetch("voices-v1.0.bin"))
    return _kokoro["model"]


def kokoro_base_voices():
    with _kokoro_lock:
        return _kokoro_model().get_voices()


def kokoro_label(voice):
    """am_michael -> 'am_michael (♂ EE.UU.)'"""
    return f"{voice} ({'♀' if voice[1] == 'f' else '♂'} {_KOKORO_ACCENT.get(voice[0], '?')})"


def kokoro_voices():
    """Cualquier voz puede hablar inglés o español; las nativas de cada idioma van primero."""
    base = kokoro_base_voices()
    names = []
    for lang, native in (("en-us", "ab"), ("es-es", "e")):
        ordered = [v for v in base if v[0] in native] + [v for v in base if v[0] not in native]
        names += [f"{lang} · ★ {b}" for b in kokoro_blends]
        names += [f"{lang} · {kokoro_label(v)}" for v in ordered]
    return names


def kokoro_blend_synth(text, parts, lang, rate=0, pitch=0):
    """parts = [(voz, peso)]: suma ponderada de los vectores de identidad de cada voz."""
    shift = 2 ** (int(pitch) / 20)
    speed = min(2.0, max(0.5, 2 ** (int(rate) / 10) / shift))
    with _kokoro_lock:
        model = _kokoro_model()
        total = sum(w for _, w in parts) or 1.0
        style = sum(model.get_voice_style(v) * (w / total) for v, w in parts)
        audio, sr = model.create(text, voice=style, speed=speed, lang="es" if lang.startswith("es") else "en-us")
    return audio.astype(np.float32)[:, None], int(sr * shift)


def kokoro_synth(text, voice_name, rate, pitch):
    """Voces neuronales Kokoro (offline). Tono: se genera más lento y se reproduce más rápido."""
    lang, voice = voice_name.split(" · ", 1)
    if voice.startswith("★ "):
        parts = kokoro_blends.get(voice[2:])
        if not parts:
            raise RuntimeError(f"La mezcla «{voice[2:]}» ya no existe")
    else:
        parts = [(voice.split(" ")[0], 1.0)]
    return kokoro_blend_synth(text, parts, lang, rate, pitch)


def _clone_voices():
    from . import clone  # importación tardía: clone usa este módulo
    return clone.clone_voices()


def _clone_synth(text, voice_name, rate, pitch):
    from . import clone
    return clone.clone_synth(text, voice_name, rate, pitch)


# motor -> (descripción, listar voces, sintetizar)
ENGINES = {
    "kokoro": ("Kokoro — neuronal offline, muy natural, ¡mezcla tus propias voces!", kokoro_voices, kokoro_synth),
    "clon": ("🧬 Voces clonadas — habla con la voz de un audio que tú elijas", _clone_voices, _clone_synth),
    "espeak": ("eSpeak NG — robots de verdad y voz tipo Stephen Hawking (Klatt)", espeak_voices, espeak_synth),
    "sapi": ("Windows SAPI — clásica robótica (estilo Loquendo)", sapi_voices, sapi_synth),
    "winrt": ("Windows OneCore — Pablo, Laura, Helena… (offline)", winrt_voices, winrt_synth),
    "piper": ("Piper — neuronal offline, muy natural (descarga cada voz)", piper_voices, piper_synth),
    "edge": ("Microsoft Edge — neuronal online", edge_voices, edge_synth),
}


def voice_language(name):
    """Deduce el idioma de una voz por su nombre: 'en', 'es' o None."""
    n = name.lower()
    for lang, word in (("en", "english"), ("es", "spanish")):
        if word in n or n.startswith((f"{lang}-", f"{lang}_", f"{lang}+")) or f"({lang}-" in n:
            return lang
    return None
