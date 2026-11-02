import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "datos"
SOUNDS_DIR = DATA_DIR / "sonidos"
MODELS_DIR = DATA_DIR / "modelos"
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULTS = {
    # Dispositivos (se guardan por nombre, porque los índices cambian)
    "cable_device": "",
    "monitor_device": "",
    "monitor_enabled": True,
    "mic_device": "",
    "cable_volume": 1.0,
    "normalize": True,  # igualar la sonoridad (LUFS) de sonidos y voces
    "monitor_volume": 0.6,
    # Panel de sonidos
    "sounds": [],  # {"id", "name", "file", "volume", "hotkey"}
    "stop_hotkey": "",
    "ytdlp_browser": "",  # navegador cuya sesión de YouTube se usa (vídeos con restricción de edad)
    # Con Bloq Mayús activado: F1 habla, F2-F9 sonidos, y esas teclas no llegan al juego
    "caps_layer": True,
    "caps_off_stops": True,  # al apagar Bloq Mayús se corta todo lo que suena
    "caps_beep": True,  # pitido al activar (sube) y desactivar (baja) Bloq Mayús
    # Tecla que Ghost Voice mantiene pulsada mientras suena algo por el cable
    # (la de "Pulsar para hablar" de Discord o del juego)
    "auto_ptt_enabled": False,
    "auto_ptt_key": "",
    "preroll_ms": 350,
    # Voz
    "ptt_key": "f1",
    "voice_effect": "normal",
    "stt_model": "small",
    "voice_mode": "en-en",
    "tts_engine": "sapi",
    "sapi_voice_en": "",
    "sapi_voice_es": "",
    "edge_voice_en": "en-US-GuyNeural",
    "edge_voice_es": "es-ES-AlvaroNeural",
    "kokoro_voice_en": "en-us · am_michael (♂ EE.UU.)",
    "kokoro_voice_es": "es-es · em_alex (♂ España)",
    "kokoro_blends": {},  # nombre -> [[voz, peso], ...]
    "clon_voice_en": "",
    "clon_voice_es": "",
    "clone": {"mode": "rapido", "steps": 6, "base_en": "am_michael", "base_es": "em_alex"},
    "espeak_voice_en": "en-us+hawking",
    "espeak_voice_es": "es+hawking",
    "winrt_voice_en": "",
    "winrt_voice_es": "Microsoft Pablo (es-ES)",
    "piper_voice_en": "en_US-ryan-high",
    "piper_voice_es": "es_ES-davefx-medium",
    "tts_rate": 0,
    "tts_pitch": 0,
    "tts_volume": 1.0,
    "phrases": [],
}


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        self._lock = threading.Lock()
        for d in (DATA_DIR, SOUNDS_DIR, MODELS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                self.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass

    def save(self):
        with self._lock:
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CONFIG_FILE)
