"""Motor de clonación de voz (Chatterbox, MIT) en su propio Python 3.11.

Ghost Voice lo arranca como subproceso y habla con él por stdin/stdout, una línea JSON por mensaje.
  {"op": "vc",  "src", "ref", "out", "steps"}            convierte un audio a la voz de "ref" (rápido)
  {"op": "tts", "text", "lang", "ref", "out", "exaggeration"}  genera la frase entera con esa voz (lento)
  {"op": "warmup", "ref"}                                 carga el conversor y calienta la gráfica
Respuesta: {"ok": true, "sr": 24000, "secs": 0.9}  o  {"ok": false, "error": "..."}
"""
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
os.environ.setdefault("HF_HOME", str(BASE / "datos" / "modelos" / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# El protocolo usa el stdout original; cualquier print de las librerías va a stderr
proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

import librosa  # noqa: E402
import torch  # noqa: E402
import torchaudio  # noqa: E402

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_models = {}
_targets = {}  # (modelo, ref, extra) ya analizado


def send(msg):
    proto.write(json.dumps(msg, ensure_ascii=False) + "\n")


def vc_model():
    if "vc" not in _models:
        from chatterbox.vc import ChatterboxVC
        _models["vc"] = ChatterboxVC.from_pretrained(device=DEVICE)
    return _models["vc"]


def tts_model():
    if "tts" not in _models:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        _models["tts"] = ChatterboxMultilingualTTS.from_pretrained(device=DEVICE)
    return _models["tts"]


def convert(req):
    from chatterbox.models.s3tokenizer import S3_SR
    m = vc_model()
    if _targets.get("vc") != req["ref"]:
        m.set_target_voice(req["ref"])
        _targets["vc"] = req["ref"]
    src = torch.from_numpy(librosa.load(req["src"], sr=S3_SR)[0]).float().to(DEVICE)[None]
    tokens, _ = m.s3gen.tokenizer(src)
    wav, _ = m.s3gen.inference(speech_tokens=tokens, ref_dict=m.ref_dict, n_cfm_timesteps=req.get("steps", 6))
    wav = wav.squeeze(0).cpu().numpy()
    return m.watermarker.apply_watermark(wav, sample_rate=m.sr), m.sr


def generate(req):
    m = tts_model()
    key = (req["ref"], req.get("exaggeration", 0.5))
    if _targets.get("tts") != key:
        m.prepare_conditionals(req["ref"], exaggeration=key[1])
        _targets["tts"] = key
    wav = m.generate(req["text"], language_id=req["lang"], exaggeration=key[1], cfg_weight=req.get("cfg_weight", 0.5))
    return wav.squeeze(0).cpu().numpy(), m.sr


def main():
    send({"ready": True, "device": DEVICE})
    for line in sys.stdin:
        try:
            req = json.loads(line)
            t = time.monotonic()
            with torch.inference_mode():
                if req["op"] == "warmup":
                    if req.get("full"):
                        generate({"text": "Ready.", "lang": "en", "ref": req["ref"]})
                    else:
                        convert({"src": req["ref"], "ref": req["ref"], "steps": 2})
                    send({"ok": True, "secs": round(time.monotonic() - t, 2)})
                    continue
                wav, sr = convert(req) if req["op"] == "vc" else generate(req)
            torchaudio.save(req["out"], torch.from_numpy(wav).float()[None], sr)
            send({"ok": True, "sr": sr, "secs": round(time.monotonic() - t, 2)})
        except Exception as e:  # el servidor sigue vivo para la siguiente frase
            send({"ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
