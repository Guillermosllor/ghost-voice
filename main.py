import os
import site
import sys
from pathlib import Path

# Con pythonw no hay consola: los errores van a datos/registro.log
if sys.stderr is None:
    log_path = Path(__file__).resolve().parent / "datos" / "registro.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(log_path, "w", encoding="utf-8", buffering=1)

# Si el programa se cae (incluso por un fallo de una librería en C), deja escrito dónde
import faulthandler  # noqa: E402
faulthandler.enable(file=sys.stderr, all_threads=True)

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# Librerías CUDA instaladas con pip (nvidia-cublas-cu12, nvidia-cudnn-cu12) para Whisper en GPU
for sp in site.getsitepackages():
    for bin_dir in Path(sp, "nvidia").glob("*/bin"):
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

from vozfantasma.ui import run  # noqa: E402

if __name__ == "__main__":
    run()
