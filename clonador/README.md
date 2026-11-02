# Motor de clonación (Python 3.11 aparte)

Chatterbox (Resemble AI, licencia MIT) necesita PyTorch, que aún no funciona con el Python 3.14 de la app.
Para reinstalarlo desde la carpeta del proyecto:

    .venv\Scripts\python.exe -m uv venv clonador\.venv --python 3.11
    .venv\Scripts\python.exe -m uv pip install --python clonador\.venv\Scripts\python.exe chatterbox-tts torch==2.6.0 torchaudio==2.6.0 "setuptools<81" --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cu124 --index-strategy unsafe-best-match

Los audios generados llevan la marca de agua inaudible Perth de Resemble AI (identifica audio sintético).
