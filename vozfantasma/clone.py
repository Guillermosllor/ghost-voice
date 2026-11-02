"""Voces clonadas: guarda audios de referencia y habla con el motor de clonación (otro Python)."""
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from . import speech
from .audio import conform, load_audio
from .config import BASE_DIR, DATA_DIR

VOICES_DIR = DATA_DIR / "voces_clonadas"
SERVER_PY = BASE_DIR / "clonador" / ".venv" / "Scripts" / "python.exe"
SERVER_SCRIPT = BASE_DIR / "clonador" / "servidor.py"
REF_MAX_SECONDS = 20

settings = {"mode": "rapido", "steps": 6, "base_en": "am_michael", "base_es": "em_alex"}  # lo rellena la UI


def available():
    return SERVER_PY.exists()


def voice_names():
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in VOICES_DIR.glob("*.wav"))


def add_voice(name, source, start=0.0, end=None):
    """Recorta un trozo de cualquier audio/vídeo y lo guarda como referencia (24 kHz mono, 32 bits)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "voz"
    data, sr = load_audio(source)
    a = int(start * sr)
    b = int(min(end if end else a / sr + REF_MAX_SECONDS, a / sr + REF_MAX_SECONDS) * sr)
    clip = conform(data[a:b], sr, 24000, 1)[:, 0]
    if len(clip) < 24000 * 3:
        raise ValueError("El trozo es demasiado corto: usa al menos 3 segundos (mejor 10-20 s)")
    clip *= 0.9 / (float(np.abs(clip).max()) or 1.0)
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    sf.write(VOICES_DIR / f"{name}.wav", clip, 24000, subtype="FLOAT")
    return name


def remove_voice(name):
    (VOICES_DIR / f"{name}.wav").unlink(missing_ok=True)


class _Server:
    """Un único proceso del motor de clonación, arrancado al primer uso."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def _start(self):
        log = open(DATA_DIR / "clonador.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(SERVER_PY), str(SERVER_SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            text=True, encoding="utf-8", bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        hello = json.loads(self.proc.stdout.readline() or "{}")
        if not hello.get("ready"):
            self.stop()
            raise RuntimeError("El motor de clonación no arrancó (mira datos/clonador.log)")

    def call(self, **req):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            line = self.proc.stdout.readline()
            if not line:
                self.proc = None
                raise RuntimeError("El motor de clonación se cerró (mira datos/clonador.log)")
            resp = json.loads(line)
            if not resp.get("ok"):
                raise RuntimeError(f"Clonación: {resp.get('error')}")
            return resp

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            finally:
                self.proc = None


server = _Server()


def warmup(name):
    server.call(op="warmup", ref=str(VOICES_DIR / f"{name}.wav"), full=settings["mode"] == "completo")


def clone_voices():
    return [f"{lang} · 🧬 {n}" for lang in ("en-us", "es-es") for n in voice_names()]


def clone_synth(text, voice_name, rate, pitch):
    lang, name = voice_name.split(" · 🧬 ", 1)
    ref = VOICES_DIR / f"{name}.wav"
    if not ref.exists():
        raise RuntimeError(f"La voz clonada «{name}» ya no existe")
    fd, out = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        if settings["mode"] == "completo":
            server.call(op="tts", text=text, lang=lang[:2], ref=str(ref), out=out)
            data, sr = sf.read(out, dtype="float32", always_2d=True)
            return data, sr
        # Rápido: Kokoro pone las palabras y la entonación; el conversor pone el timbre de la voz clonada.
        # El tono se ajusta con el truco de generar más lento y reproducir más rápido.
        base = settings["base_es" if lang.startswith("es") else "base_en"]
        shift = 2 ** (int(pitch) / 20)
        src, declared_sr = speech.kokoro_blend_synth(text, [(base, 1.0)], lang, rate, pitch)  # ya ralentizada
        fd, src_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            sf.write(src_path, src, round(declared_sr / shift), subtype="FLOAT")
            server.call(op="vc", src=src_path, ref=str(ref), out=out, steps=settings["steps"])
        finally:
            os.remove(src_path)
        data, sr = sf.read(out, dtype="float32", always_2d=True)
        return data, int(sr * shift)
    finally:
        os.remove(out)
