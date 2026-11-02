"""Biblioteca de sonidos: importar archivos y descargar desde internet."""
import os
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
import soundfile as sf

from .audio import conform, load_audio
from .config import SOUNDS_DIR

AUDIO_EXTS = {".wav", ".mp3", ".mp2", ".mp1", ".mpga", ".mpa", ".ogg", ".oga", ".flac", ".m4a", ".opus",
              ".aac", ".ac3", ".wma", ".aiff", ".aif", ".amr", ".mka", ".caf"}
# De los vídeos solo se guarda el audio, extraído a FLAC (sin pérdida)
VIDEO_EXTS = {".mpeg", ".mpg", ".mpe", ".m1v", ".m2v", ".vob", ".ts", ".mts", ".m2ts", ".mp4", ".m4v", ".mkv",
              ".webm", ".avi", ".mov", ".wmv", ".flv", ".3gp", ".ogv"}
ALL_EXTS = AUDIO_EXTS | VIDEO_EXTS


def _safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:80] or "sonido"


def _unique_path(stem, ext):
    path = SOUNDS_DIR / f"{stem}{ext}"
    n = 2
    while path.exists():
        path = SOUNDS_DIR / f"{stem} ({n}){ext}"
        n += 1
    return path


def make_entry(path, name=None):
    load_audio(path)  # valida que se puede decodificar
    return {"id": uuid.uuid4().hex, "name": name or Path(path).stem, "file": Path(path).name,
            "volume": 1.0, "hotkey": ""}


def _extract_audio(path, stem):
    """Saca la pista de audio de un vídeo a FLAC de 24 bits y borra el original descargado/copiado."""
    data, sr = load_audio(path)
    dst = _unique_path(stem, ".flac")
    sf.write(dst, data, sr, subtype="PCM_24")
    return dst


def save_generated(data, sr, text, volume=1.0):
    """Guarda una frase sintetizada como sonido del panel (FLAC 24 bits a 48 kHz, sin pérdida)."""
    name = " ".join(text.split())
    name = name if len(name) <= 40 else name[:39] + "…"
    clip = conform(data, sr, 48000, 1)
    dst = _unique_path(_safe_name("voz - " + name), ".flac")
    sf.write(dst, clip, 48000, subtype="PCM_24")
    entry = make_entry(dst, "🗣 " + name)
    entry["volume"] = volume
    return entry


def import_file(src):
    src = Path(src)
    stem = _safe_name(src.stem)
    if src.suffix.lower() in VIDEO_EXTS:
        return make_entry(_extract_audio(src, stem), src.stem)
    dst = _unique_path(stem, src.suffix.lower())
    shutil.copy2(src, dst)
    try:
        return make_entry(dst, src.stem)
    except Exception:
        dst.unlink(missing_ok=True)
        raise


_APPDATA, _LOCAL = os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")
# nombre visible -> (navegador para yt-dlp, carpeta de perfil o None, carpeta que indica que está instalado)
BROWSERS = {
    "Opera GX": ("opera", rf"{_APPDATA}\Opera Software\Opera GX Stable", rf"{_APPDATA}\Opera Software\Opera GX Stable"),
    "Opera": ("opera", None, rf"{_APPDATA}\Opera Software\Opera Stable"),
    "Chrome": ("chrome", None, rf"{_LOCAL}\Google\Chrome\User Data"),
    "Edge": ("edge", None, rf"{_LOCAL}\Microsoft\Edge\User Data"),
    "Brave": ("brave", None, rf"{_LOCAL}\BraveSoftware\Brave-Browser\User Data"),
    "Firefox": ("firefox", None, rf"{_APPDATA}\Mozilla\Firefox\Profiles"),
}


def installed_browsers():
    return [name for name, (_b, _p, marker) in BROWSERS.items() if os.path.isdir(marker)]


class NeedsLogin(Exception):
    """YouTube pide iniciar sesión (restricción de edad, etc.): hay que usar la sesión del navegador."""


class BrowserLocked(Exception):
    """El navegador está abierto y bloquea su archivo de sesión (pasa con los basados en Chrome)."""


BROWSER_EXES = {"Opera GX": "opera.exe", "Opera": "opera.exe", "Chrome": "chrome.exe", "Edge": "msedge.exe",
                "Brave": "brave.exe", "Firefox": "firefox.exe"}


def browser_running(browser):
    """¿Sigue abierto (aunque sea en segundo plano, junto al reloj)?"""
    exe = BROWSER_EXES.get(browser)
    if not exe:
        return False
    import subprocess
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"], capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return exe.lower() in out.lower()


def friendly_error(e):
    """Traduce los errores típicos de yt-dlp a algo entendible."""
    msg = str(e)
    rules = [
        ("confirm your age", "El vídeo tiene restricción de edad: hace falta tu sesión de YouTube."),
        ("Sign in to confirm you", "YouTube pide iniciar sesión para este vídeo."),
        ("Private video", "Es un vídeo privado."),
        ("Video unavailable", "El vídeo no está disponible (borrado o bloqueado en tu país)."),
        ("could not find", "No se pudo leer la sesión del navegador. Prueba con otro navegador o ciérralo y reintenta."),
        ("Failed to decrypt", "No se pudo leer la sesión del navegador (cifrado nuevo de Chrome/Edge). "
                              "Prueba con Opera GX o Firefox."),
        ("Could not copy", "El navegador tiene bloqueadas sus cookies: ciérralo del todo y vuelve a intentarlo."),
        ("Unsupported URL", "Ese enlace no es de una web compatible."),
        ("File is larger than max-filesize", "El archivo es demasiado grande (máx. 200 MB)."),
    ]
    for needle, text in rules:
        if needle.lower() in msg.lower():
            return text
    return msg.splitlines()[0][:300]


def download(url, progress=None, browser=""):
    """Descarga un enlace directo a un audio, o usa yt-dlp para YouTube y otras webs.
    progress(fracción 0-1 o None, texto) se llama durante la descarga."""
    progress = progress or (lambda _f, _t: None)
    url = url.strip()
    ext = Path(unquote(urlparse(url).path)).suffix.lower()
    if ext in ALL_EXTS:
        return _download_direct(url, ext, progress)
    try:
        head = requests.head(url, allow_redirects=True, timeout=10)
        if head.headers.get("content-type", "").startswith("audio/"):
            return _download_direct(url, ext or ".mp3", progress)
    except requests.RequestException:
        pass
    return _download_ytdlp(url, progress, browser)


def _human(n):
    return f"{n / 1024 / 1024:.1f} MB"


def _download_direct(url, ext, progress):
    stem = _safe_name(Path(unquote(urlparse(url).path)).stem)
    dst = _unique_path(stem, ext)
    with requests.get(url, stream=True, timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        total, done = int(r.headers.get("content-length") or 0), 0
        with open(dst, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                done += len(chunk)
                progress(done / total if total else None, f"{_human(done)}" + (f" de {_human(total)}" if total else ""))
    progress(None, "Procesando…")
    try:
        if ext in VIDEO_EXTS:
            video, dst = dst, _extract_audio(dst, stem)
            video.unlink(missing_ok=True)
        return make_entry(dst, stem)
    except Exception:
        dst.unlink(missing_ok=True)
        raise


def _download_ytdlp(url, progress, browser):
    import yt_dlp

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            extra = []
            if d.get("speed"):
                extra.append(f"{d['speed'] / 1024 / 1024:.1f} MB/s")
            if d.get("eta") is not None:
                extra.append(f"quedan {int(d['eta'])} s")
            text = _human(done) + (f" de {_human(total)}" if total else "")
            progress(done / total if total else None, text + (" · " + " · ".join(extra) if extra else ""))
        elif d["status"] == "finished":
            progress(None, "Procesando…")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(SOUNDS_DIR / "%(title).70s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # el progreso lo pinta nuestra barra
        "max_filesize": 200 * 1024 * 1024,
        "progress_hooks": [hook],
    }
    try:  # Deno resuelve el JavaScript de YouTube (sin él faltan formatos)
        import deno
        opts["js_runtimes"] = {"deno": {"path": deno.find_deno_bin()}}
    except Exception:
        pass
    if browser in BROWSERS:
        name, profile, _marker = BROWSERS[browser]
        opts["cookiesfrombrowser"] = (name, profile, None, None)
    progress(None, "Buscando el vídeo…")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        text = str(e)
        if not browser and ("confirm your age" in text or "Sign in to confirm" in text):
            raise NeedsLogin(friendly_error(e)) from e
        if "Could not copy" in text:
            raise BrowserLocked(browser) from e
        raise RuntimeError(friendly_error(e)) from e
    try:
        return make_entry(path, info.get("title") or path.stem)
    except Exception:
        path.unlink(missing_ok=True)
        raise
