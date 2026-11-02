"""Efectos de voz graciosos (pedalboard de Spotify, calidad de estudio).

Se procesa a 48 kHz para que la distorsión y el cambio de tono no generen aliasing.
Algunos efectos aceleran/ralentizan (estilo ardilla) declarando otra frecuencia de muestreo:
el remuestreo VHQ posterior hace el cambio sin pérdida de calidad.
"""
import numpy as np
from pedalboard import (
    Bitcrush, Chorus, Compressor, Delay, Distortion, GSMFullRateCompressor, HighpassFilter,
    LowpassFilter, LowShelfFilter, Mix, Pedalboard, Phaser, PitchShift, Reverb,
)

from .audio import conform

SR = 48000


def _ring(x, freq, amount):
    t = np.arange(len(x)) / SR
    return x * (1 - amount) + x * np.sin(2 * np.pi * freq * t) * amount


def _pitufo(x):
    return Pedalboard([PitchShift(5), HighpassFilter(150)])(x, SR), 1.35


def _rudo(x):
    board = Pedalboard([
        PitchShift(-4),
        LowShelfFilter(cutoff_frequency_hz=180, gain_db=5),
        Distortion(drive_db=7),
        LowpassFilter(7000),
        Compressor(threshold_db=-20, ratio=4, attack_ms=5, release_ms=120),
    ])
    return board(x, SR), 1.0


def _gigante(x):
    board = Pedalboard([PitchShift(-3), LowShelfFilter(cutoff_frequency_hz=150, gain_db=4),
                        Reverb(room_size=0.7, wet_level=0.25, dry_level=0.9)])
    return board(x, SR), 0.78


def _demonio(x):
    board = Pedalboard([
        Mix([Pedalboard([PitchShift(-12)]), Pedalboard([PitchShift(-7)])]),
        Distortion(drive_db=10),
        LowpassFilter(6000),
        Reverb(room_size=0.85, wet_level=0.3, dry_level=0.8),
    ])
    return board(x, SR), 1.0


def _robot(x):
    x = _ring(x, 55, 0.8)
    board = Pedalboard([Delay(delay_seconds=0.011, feedback=0.55, mix=0.45), Bitcrush(bit_depth=10),
                        HighpassFilter(120)])
    return board(x.astype(np.float32), SR), 1.0


def _fantasma(x):
    board = Pedalboard([
        PitchShift(-2),
        HighpassFilter(200),
        Chorus(rate_hz=0.6, depth=0.4, mix=0.5),
        Delay(delay_seconds=0.33, feedback=0.35, mix=0.3),
        Reverb(room_size=0.95, damping=0.3, wet_level=0.5, dry_level=0.6),
    ])
    return board(x, SR), 1.0


def _alien(x):
    board = Pedalboard([PitchShift(4), Phaser(rate_hz=1.5, depth=0.8, mix=0.7),
                        Chorus(rate_hz=3, depth=0.5, mix=0.4), Delay(delay_seconds=0.06, feedback=0.3, mix=0.25)])
    return board(x, SR), 1.0


def _abuelo(x):
    # Chorus 100 % húmedo y rápido = vibrato tembloroso
    board = Pedalboard([PitchShift(1), Chorus(rate_hz=6, depth=0.35, centre_delay_ms=6, mix=1.0),
                        HighpassFilter(180)])
    return board(x, SR), 0.92


def _walkie(x):
    board = Pedalboard([HighpassFilter(450), LowpassFilter(3200), Distortion(drive_db=14),
                        GSMFullRateCompressor(), Compressor(threshold_db=-18, ratio=6)])
    y = board(x, SR)
    # "kssht" al soltar el botón
    rng = np.random.default_rng()
    hiss = (rng.standard_normal(int(SR * 0.18)) * 0.12).astype(np.float32)
    hiss = Pedalboard([HighpassFilter(1500), LowpassFilter(5000)])(hiss, SR)
    return np.concatenate([y, hiss]), 1.0


# clave -> (nombre visible, función)
PRESETS = {
    "normal": ("Normal (sin efecto)", None),
    "pitufo": ("🔵 Pitufo / ardilla", _pitufo),
    "rudo": ("💪 Hombre rudo", _rudo),
    "gigante": ("🗿 Gigante lento", _gigante),
    "demonio": ("😈 Demonio", _demonio),
    "robot": ("🤖 Robot", _robot),
    "fantasma": ("👻 Fantasma", _fantasma),
    "alien": ("👽 Alien", _alien),
    "abuelo": ("👴 Abuelo tembloroso", _abuelo),
    "walkie": ("📻 Walkie-talkie", _walkie),
}


def apply(preset, data, sr):
    """Aplica el efecto. Devuelve (audio, frecuencia declarada)."""
    fn = PRESETS.get(preset, PRESETS["normal"])[1]
    if fn is None:
        return data, sr
    x = conform(data, sr, SR, 1)[:, 0]
    y, speed = fn(x)
    peak = float(np.abs(y).max()) or 1.0
    y = (y * (0.9 / peak)).astype(np.float32)  # mismo volumen percibido para todos los efectos
    return y[:, None], int(SR * speed)
