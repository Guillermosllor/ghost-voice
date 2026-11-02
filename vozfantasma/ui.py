"""Interfaz gráfica (PySide6)."""
import ctypes
import os
import queue
import tempfile
import threading
import time
import traceback
from html import escape
from pathlib import Path

import keyboard
import numpy as np
import soundfile as sf
from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QToolButton, QSlider, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from . import audio, clone, effects, library, speech
from .config import SOUNDS_DIR, Config

STYLE = """
* { font-family: 'Segoe UI'; font-size: 10pt; color: #e6e6eb; }
QMainWindow, QWidget { background: #17171c; }
QTabWidget::pane { border: 1px solid #2c2c35; border-radius: 6px; }
QTabBar::tab { background: #22222a; padding: 8px 18px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background: #6d4aff; }
QGroupBox { border: 1px solid #2c2c35; border-radius: 8px; margin-top: 14px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #a993ff; }
QPushButton { background: #2a2a34; border: 1px solid #3a3a46; border-radius: 6px; padding: 6px 12px; }
QPushButton:hover { background: #363643; }
QPushButton:pressed { background: #6d4aff; }
QPushButton#sound { background: #24243a; border: 1px solid #3d3d66; min-height: 64px; font-weight: 600; }
QPushButton#sound:hover { background: #31315a; }
QPushButton#sound[playing="true"] { border: 2px solid #7dffa8; background: #1f3a30; }
QToolButton#trim { background: transparent; border: none; color: #9a9aa8; font-size: 12pt; border-radius: 5px; }
QToolButton#trim:hover { background: #6d4aff; color: #ffffff; }
QPushButton#ptt { min-height: 70px; font-size: 13pt; font-weight: 700; background: #2b2050; }
QPushButton#ptt[live="true"] { background: #c22b4a; }
QLineEdit, QComboBox, QTextEdit, QListWidget, QDoubleSpinBox { background: #22222a; border: 1px solid #3a3a46; border-radius: 6px; padding: 5px; }
QComboBox QAbstractItemView { background: #22222a; selection-background-color: #6d4aff; }
QSlider::groove:horizontal { height: 6px; background: #2c2c35; border-radius: 3px; }
QSlider::handle:horizontal { background: #a993ff; width: 14px; margin: -5px 0; border-radius: 7px; }
QScrollArea { border: none; }
QProgressBar { background: #22222a; border: 1px solid #3a3a46; border-radius: 6px; text-align: center; min-height: 18px; }
QProgressBar::chunk { background: #6d4aff; border-radius: 5px; }
QLabel#status { color: #9a9aa8; padding: 4px; }
QLabel#warn { color: #ffb347; }
"""


NONE = "(ninguno)"
SOUND_FKEYS = [f"f{i}" for i in range(2, 10)]


def _repeat_delay():
    """Espera de Windows antes de autorrepetir una tecla mantenida (0-3 -> 250-1000 ms)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Keyboard") as k:
            return (int(winreg.QueryValueEx(k, "KeyboardDelay")[0]) + 1) * 0.25
    except Exception:
        return 1.0


REPEAT_DELAY = _repeat_delay()


def caps_lock_on():
    return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)


class Bridge(QObject):
    """Señales para pasar eventos de hilos secundarios (teclado, IA) a la interfaz."""
    play = Signal(str)
    stop = Signal()
    ptt = Signal(bool)
    status = Signal(str)
    log = Signal(str)
    error = Signal(str)
    voices = Signal(str, list)
    sound_added = Signal(dict)
    hotkey_captured = Signal(str, str)
    dl_progress = Signal(int, str)  # 0-1000, -1 = sin porcentaje, -2 = terminado
    need_login = Signal(str, str)
    browser_locked = Signal(str, str)
    dl_failed = Signal(str, str)


def _slider(lo, hi, value):
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    return s


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = Config()
        speech.kokoro_blends = self.cfg["kokoro_blends"]  # el mismo diccionario: los cambios se ven al momento
        clone.settings = self.cfg["clone"]
        self.engine = audio.AudioEngine()
        self.recorder = audio.Recorder()
        self.stt = speech.Transcriber()
        self.translator = speech.Translator()
        self.bridge = Bridge()
        self.jobs = queue.Queue()
        self._last_tts = None  # (audio, frecuencia, texto) de la última frase dicha
        self._caps_prev = caps_lock_on()
        self._generation = 0  # sube con cada corte: las frases de antes ya no suenan
        self._ptt_down = False
        self._ptt_by_key = False
        self._ptt_watchdog_armed = False
        self._ptt_down_at = 0.0
        self._ptt_last_event = 0.0
        self._held = set()

        self.setWindowTitle("Ghost Voice 👻")
        self.resize(980, 680)
        self.setAcceptDrops(True)

        tabs = QTabWidget()
        tabs.addTab(self._build_sounds_tab(), "🔊  Sonidos")
        tabs.addTab(self._build_voice_tab(), "🗣  Voz")
        tabs.addTab(self._build_audio_tab(), "⚙  Audio")
        self.status = QLabel()
        self.status.setObjectName("status")
        self.layer_label = QLabel()
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.layer_label)
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addWidget(tabs)
        lay.addLayout(bottom)
        self.setCentralWidget(central)

        b = self.bridge
        b.play.connect(self.play_sound)
        b.stop.connect(self.engine.stop_all)
        b.ptt.connect(self._on_ptt_key)
        b.status.connect(self.status.setText)
        b.log.connect(self.log.append)
        b.error.connect(lambda m: self.log.append(f"<span style='color:#ff6b81'>⚠ {m}</span>"))
        b.voices.connect(self._fill_voices)
        b.sound_added.connect(self._add_entry)
        b.hotkey_captured.connect(self._on_hotkey_captured)
        b.dl_progress.connect(self._on_dl_progress)
        b.need_login.connect(self._on_need_login)
        b.browser_locked.connect(self._on_browser_locked)
        b.dl_failed.connect(self._on_dl_failed)

        threading.Thread(target=self._voice_worker, daemon=True).start()
        self.refresh_devices()
        self._assign_fkeys()
        self.rebuild_grid()
        self.register_hotkeys()
        self._measure_loudness([s for s in self.cfg["sounds"] if "norm_gain" not in s])
        self._layer_timer = QTimer(self)
        self._layer_timer.timeout.connect(self._update_layer_label)
        self._layer_timer.start(250)
        self._update_layer_label()
        self._auto_ptt_down = None  # tecla actualmente mantenida por nosotros
        self._auto_ptt_last = 0.0
        self._auto_ptt_timer = QTimer(self)
        self._auto_ptt_timer.timeout.connect(self._auto_ptt_tick)
        self._auto_ptt_timer.start(20)
        self._apply_auto_ptt()
        self._warm_timer = QTimer(self)
        self._warm_timer.timeout.connect(self._keep_warm)
        self._warm_timer.start(15000)
        self._playing_timer = QTimer(self)
        self._playing_timer.timeout.connect(self._update_playing)
        self._playing_timer.start(50)
        self.load_stt_model()
        self._preload_translator()
        self._load_voices()
        self._warmup_clone()

    # ================================================================ pestañas

    def _build_sounds_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        add = QPushButton("＋ Añadir archivos")
        add.clicked.connect(self.add_files)
        dl = QPushButton("⬇ Descargar de internet")
        dl.clicked.connect(self.download_url)
        stop = QPushButton("■ Detener todo")
        stop.clicked.connect(self.engine.stop_all)
        self.filter = QLineEdit(placeholderText="Buscar…")
        self.filter.textChanged.connect(self.rebuild_grid)
        for x in (add, dl, stop):
            bar.addWidget(x)
        bar.addWidget(self.filter, 1)
        lay.addLayout(bar)
        hint = QLabel("Clic = reproducir · ✂ (esquina) = recortar · Clic derecho = renombrar, volumen, atajo, "
                      "eliminar · Arrastra archivos a la ventana para añadirlos.")
        hint.setObjectName("status")
        lay.addWidget(hint)
        self.dl_box = QWidget()
        r = QHBoxLayout(self.dl_box)
        r.setContentsMargins(0, 0, 0, 0)
        self.dl_label = QLabel()
        self.dl_bar = QProgressBar()
        self.dl_bar.setTextVisible(True)
        r.addWidget(QLabel("⬇"))
        r.addWidget(self.dl_bar, 1)
        r.addWidget(self.dl_label)
        self.dl_box.hide()
        lay.addWidget(self.dl_box)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.grid_scroll = scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # los botones se recolocan, nunca se salen
        scroll.setWidget(self.grid_host)
        scroll.viewport().installEventFilter(self)
        lay.addWidget(scroll, 1)
        self.sound_buttons = {}
        return w

    def _build_voice_tab(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()

        g = QGroupBox("Repetir lo que digo (pulsar para hablar)")
        f = QFormLayout(g)
        self.ptt_btn = QPushButton("Mantén pulsado para hablar")
        self.ptt_btn.setObjectName("ptt")
        self.ptt_btn.pressed.connect(lambda: self._on_ptt(True))
        self.ptt_btn.released.connect(lambda: self._on_ptt(False))
        f.addRow(self.ptt_btn)
        self.ptt_key_btn = QPushButton(self.cfg["ptt_key"] or "(ninguna)")
        self.ptt_key_btn.clicked.connect(lambda: self.capture_hotkey("ptt", single=True))
        f.addRow("Tecla para hablar:", self.ptt_key_btn)
        self.model_box = QComboBox()
        self.model_box.addItems(speech.STT_MODELS)
        self.model_box.setCurrentText(self.cfg["stt_model"])
        self.model_box.currentTextChanged.connect(lambda t: (self._set("stt_model", t), self.load_stt_model()))
        f.addRow("Modelo Whisper:", self.model_box)
        left.addWidget(g)

        g = QGroupBox("Modo de idioma (hablo → sale)")
        f = QFormLayout(g)
        self.mode_box = QComboBox()
        for code, (_src, _dst, label) in speech.MODES.items():
            self.mode_box.addItem(label, code)
        self.mode_box.setCurrentIndex(list(speech.MODES).index(self.cfg["voice_mode"]))
        self.mode_box.currentIndexChanged.connect(self._mode_changed)
        f.addRow("Modo:", self.mode_box)
        hint = QLabel("También se aplica a lo que escribas. La traducción es local (sin internet).")
        hint.setObjectName("status")
        f.addRow(hint)
        left.addWidget(g)

        g = QGroupBox("Voz sintética")
        f = QFormLayout(g)
        self.my_voices = QComboBox()
        self.my_voices.activated.connect(lambda: self.use_my_voice(self.my_voices.currentData()))
        f.addRow("⭐ Mis voces:", self.my_voices)
        self._refresh_my_voices()
        self.engine_box = QComboBox()
        for code, (label, _list, _synth) in speech.ENGINES.items():
            self.engine_box.addItem(label, code)
        self.engine_box.setCurrentIndex(list(speech.ENGINES).index(self.cfg["tts_engine"]))
        self.engine_box.currentIndexChanged.connect(self._engine_changed)
        f.addRow("Motor:", self.engine_box)
        self.voice_boxes = {}
        for lang, label in (("en", "Voz en inglés:"), ("es", "Voz en español:")):
            box = QComboBox()
            box.currentTextChanged.connect(lambda name, l=lang: self._voice_changed(l, name))
            f.addRow(label, box)
            self.voice_boxes[lang] = box
        for key, label in (("tts_rate", "Velocidad:"), ("tts_pitch", "Tono:")):
            s = _slider(-10, 10, self.cfg[key])
            s.valueChanged.connect(lambda v, k=key: self._set(k, v))
            f.addRow(label, s)
        s = _slider(0, 200, int(self.cfg["tts_volume"] * 100))
        s.valueChanged.connect(lambda v: self._set("tts_volume", v / 100))
        f.addRow("Volumen voz:", s)
        row = QHBoxLayout()
        self.effect_box = QComboBox()
        for code, (label, _fn) in effects.PRESETS.items():
            self.effect_box.addItem(label, code)
        self.effect_box.setCurrentIndex(list(effects.PRESETS).index(self.cfg["voice_effect"]))
        self.effect_box.currentIndexChanged.connect(lambda: self._set("voice_effect", self.effect_box.currentData()))
        test = QPushButton("▶ Probar")
        test.clicked.connect(self._test_voice)
        row.addWidget(self.effect_box, 1)
        row.addWidget(test)
        f.addRow("Efecto gracioso:", row)
        blend = QPushButton("🎛 Crear / editar mezclas de voces (Kokoro)…")
        blend.clicked.connect(self._open_blend_dialog)
        cloned = QPushButton("🧬 Voces clonadas…")
        cloned.clicked.connect(lambda: (CloneDialog(self).exec(), self._refresh_my_voices(), self._load_voices()))
        row = QHBoxLayout()
        row.addWidget(blend, 1)
        row.addWidget(cloned, 1)
        f.addRow(row)
        left.addWidget(g)
        left.addStretch()
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        g = QGroupBox("Escribir y que lo diga")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.say_edit = QLineEdit(placeholderText="Escribe algo y pulsa Enter…")
        self.say_edit.returnPressed.connect(self.say_typed)
        say = QPushButton("Decir")
        say.clicked.connect(self.say_typed)
        save = QPushButton("★ Guardar")
        save.clicked.connect(self.save_phrase)
        row.addWidget(self.say_edit, 1)
        row.addWidget(say)
        row.addWidget(save)
        v.addLayout(row)
        as_sound = QPushButton("🔊 Guardar como sonido del panel (con la voz y efecto actuales)")
        as_sound.clicked.connect(lambda: self.save_text_as_sound())
        v.addWidget(as_sound)
        self.phrases = QListWidget()
        self.phrases.addItems(self.cfg["phrases"])
        self.phrases.itemDoubleClicked.connect(lambda it: self.speak(it.text()))
        self.phrases.setContextMenuPolicy(Qt.CustomContextMenu)
        self.phrases.customContextMenuRequested.connect(self._phrase_menu)
        v.addWidget(QLabel("Frases guardadas (doble clic para decirla):"))
        v.addWidget(self.phrases)
        right.addWidget(g, 1)
        g = QGroupBox("Registro")
        v = QVBoxLayout(g)
        self.log = QTextEdit(readOnly=True)
        v.addWidget(self.log)
        last = QPushButton("💾 Guardar la última frase dicha como sonido")
        last.clicked.connect(self.save_last_as_sound)
        v.addWidget(last)
        right.addWidget(g, 1)
        lay.addLayout(right, 1)
        return w

    def _build_audio_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("Micrófono virtual (lo que oyen en el juego)")
        f = QFormLayout(g)
        self.cable_box = QComboBox()
        self.cable_box.activated.connect(lambda: self._device_changed("cable_device", self.cable_box))
        f.addRow("Salida:", self.cable_box)
        row = QHBoxLayout()
        s = _slider(0, 200, int(self.cfg["cable_volume"] * 100))
        vol_label = QLabel()

        def show_volume(v):
            vol_label.setText(f"{v} %" + ("  ⚠ por encima de 100 % se comprime el sonido" if v > 100 else ""))
        s.valueChanged.connect(lambda v: (self._set("cable_volume", v / 100), self._apply_volumes(), show_volume(v)))
        show_volume(s.value())
        row.addWidget(s, 1)
        row.addWidget(vol_label)
        f.addRow("Volumen:", row)
        norm = QCheckBox("Igualar el volumen de todos los sonidos y voces (sonoridad LUFS, recomendado)")
        norm.setChecked(self.cfg["normalize"])
        norm.toggled.connect(lambda v: self._set("normalize", v))
        f.addRow(norm)
        self.cable_warn = QLabel()
        self.cable_warn.setObjectName("warn")
        self.cable_warn.setWordWrap(True)
        f.addRow(self.cable_warn)
        info = QLabel("Elige <b>CABLE Input (VB-Audio Virtual Cable)</b> aquí, y en el juego/Discord "
                      "pon como micrófono <b>CABLE Output</b>.")
        info.setWordWrap(True)
        f.addRow(info)
        lay.addWidget(g)

        g = QGroupBox("Escucharme a mí mismo (monitor)")
        f = QFormLayout(g)
        self.mon_chk = QCheckBox("Oír también por mis auriculares lo que se envía")
        self.mon_chk.setChecked(self.cfg["monitor_enabled"])
        self.mon_chk.toggled.connect(lambda v: (self._set("monitor_enabled", v), self._apply_volumes()))
        f.addRow(self.mon_chk)
        self.monitor_box = QComboBox()
        self.monitor_box.activated.connect(lambda: self._device_changed("monitor_device", self.monitor_box))
        f.addRow("Auriculares:", self.monitor_box)
        s = _slider(0, 200, int(self.cfg["monitor_volume"] * 100))
        s.valueChanged.connect(lambda v: (self._set("monitor_volume", v / 100), self._apply_volumes()))
        f.addRow("Volumen:", s)
        lay.addWidget(g)

        g = QGroupBox("Mi micrófono real (para reconocer lo que digo)")
        f = QFormLayout(g)
        self.mic_box = QComboBox()
        self.mic_box.activated.connect(lambda: self._set("mic_device", self.mic_box.currentText()))
        f.addRow("Micrófono:", self.mic_box)
        lay.addWidget(g)

        g = QGroupBox("Atajos")
        f = QFormLayout(g)
        caps = QCheckBox("Usar Bloq Mayús como interruptor: con Bloq Mayús activado, F1 habla y F2–F9 "
                         "lanzan sonidos sin llegar al juego; apagado, las teclas funcionan normal")
        caps.setChecked(self.cfg["caps_layer"])
        caps.toggled.connect(lambda v: (self._set("caps_layer", v), self._update_layer_label()))
        f.addRow(caps)
        cut = QCheckBox("Al apagar Bloq Mayús, cortar al instante todo lo que esté sonando")
        cut.setChecked(self.cfg["caps_off_stops"])
        cut.toggled.connect(lambda v: self._set("caps_off_stops", v))
        f.addRow(cut)
        beep = QCheckBox("Pitido al activar (tonos que suben) y desactivar (tonos que bajan) Bloq Mayús")
        beep.setChecked(self.cfg["caps_beep"])
        beep.toggled.connect(lambda v: self._set("caps_beep", v))
        f.addRow(beep)
        self.stop_key_btn = QPushButton(self.cfg["stop_hotkey"] or "(ninguno)")
        self.stop_key_btn.clicked.connect(lambda: self.capture_hotkey("stop"))
        f.addRow("Detener todo:", self.stop_key_btn)
        lay.addWidget(g)

        g = QGroupBox("Pulsar para hablar automático (Discord / juegos)")
        f = QFormLayout(g)
        info = QLabel("Discord y muchos juegos cortan las voces de robot y los sonidos porque no los reconocen "
                      "como voz. Pon Discord en <b>Pulsar para hablar</b> con una tecla que no uses (por ejemplo "
                      "<b>Bloq Despl</b> o <b>Pausa</b>) y elige aquí la misma: Ghost Voice la mantendrá pulsada "
                      "mientras suena algo, y se transmitirá entero.")
        info.setWordWrap(True)
        f.addRow(info)
        auto = QCheckBox("Activar pulsar para hablar automático")
        auto.setChecked(self.cfg["auto_ptt_enabled"])
        auto.toggled.connect(lambda v: (self._set("auto_ptt_enabled", v), self._apply_auto_ptt()))
        f.addRow(auto)
        self.auto_ptt_btn = QPushButton(self.cfg["auto_ptt_key"] or "(elegir tecla)")
        self.auto_ptt_btn.clicked.connect(lambda: self.capture_hotkey("auto_ptt", single=True))
        f.addRow("Tecla de hablar de Discord/juego:", self.auto_ptt_btn)
        row = QHBoxLayout()
        pre = _slider(0, 1000, self.cfg["preroll_ms"])
        pre.setSingleStep(50)
        pre_label = QLabel(f"{self.cfg['preroll_ms']} ms")
        pre.valueChanged.connect(lambda v: (self._set("preroll_ms", v), pre_label.setText(f"{v} ms"),
                                            self._apply_auto_ptt()))
        row.addWidget(pre, 1)
        row.addWidget(pre_label)
        f.addRow("Margen antes del audio:", row)
        hint = QLabel("Si a tus amigos les llega cortado el principio, sube el margen.")
        hint.setObjectName("status")
        f.addRow(hint)
        lay.addWidget(g)

        refresh = QPushButton("↻ Actualizar dispositivos")
        refresh.clicked.connect(self.refresh_devices)
        lay.addWidget(refresh, 0, Qt.AlignLeft)
        lay.addStretch()
        return w

    # ============================================================== utilidades

    def _set(self, key, value):
        self.cfg[key] = value
        self.cfg.save()

    def _run(self, fn, *args):
        def wrapper():
            try:
                fn(*args)
            except Exception as e:
                traceback.print_exc()
                self.bridge.error.emit(str(e))
        threading.Thread(target=wrapper, daemon=True).start()

    def _entry(self, sid):
        return next((s for s in self.cfg["sounds"] if s["id"] == sid), None)

    # ============================================================ dispositivos

    def refresh_devices(self):
        outs = [n for _, n in audio.list_devices("output")]
        ins = [n for _, n in audio.list_devices("input")]
        if not self.cfg["cable_device"] or self.cfg["cable_device"] not in outs:
            self.cfg["cable_device"] = audio.find_cable()
        if not self.cfg["monitor_device"] or self.cfg["monitor_device"] not in outs:
            self.cfg["monitor_device"] = audio.device_name(audio.default_device("output"))
        if not self.cfg["mic_device"] or self.cfg["mic_device"] not in ins:
            self.cfg["mic_device"] = audio.device_name(audio.default_device("input"))
        self.cfg.save()
        for box, items, key in ((self.cable_box, [NONE] + outs, "cable_device"),
                                (self.monitor_box, outs, "monitor_device"),
                                (self.mic_box, ins, "mic_device")):
            box.clear()
            box.addItems(items)
            box.setCurrentText(self.cfg[key] or NONE)
        self._open_outputs()

    def _device_changed(self, key, box):
        self._set(key, "" if box.currentText() == NONE else box.currentText())
        self._open_outputs()

    def _open_outputs(self):
        for out, key in ((self.engine.cable, "cable_device"), (self.engine.monitor, "monitor_device")):
            try:
                out.open(audio.find_device(self.cfg[key], "output"))
            except Exception as e:
                out.close()
                self.bridge.error.emit(f"No se pudo abrir «{self.cfg[key]}»: {e}")
        if self.cfg["cable_device"] and self.cfg["cable_device"] == self.cfg["monitor_device"]:
            self.engine.monitor.close()  # evitar sonar dos veces en el mismo dispositivo
        self._apply_volumes()
        if "CABLE" not in self.cfg["cable_device"]:
            self.cable_warn.setText("⚠ No se detecta VB-Audio Virtual Cable. Instálalo (gratis) desde "
                                    "vb-audio.com/Cable y pulsa «Actualizar dispositivos».")
        else:
            self.cable_warn.setText("")
        c, m = self.engine.cable, self.engine.monitor
        print(f"[audio] cable={self.cfg['cable_device'] or '-'} abierto={c.stream is not None} · "
              f"auriculares={self.cfg['monitor_device']} abierto={m.stream is not None} "
              f"activo={m.enabled} vol={m.volume}", flush=True)
        self.bridge.status.emit(
            f"Micrófono virtual: {self.cfg['cable_device'] or '—'} ({c.sr} Hz, float32)" if c.stream
            else "Micrófono virtual sin configurar")

    def _apply_volumes(self):
        self.engine.cable.volume = self.cfg["cable_volume"]
        self.engine.monitor.volume = self.cfg["monitor_volume"]
        self.engine.monitor.enabled = self.cfg["monitor_enabled"]

    # ================================================================ sonidos

    def rebuild_grid(self):
        while self.grid.count():
            self.grid.takeAt(0).widget().deleteLater()
        text = self.filter.text().lower()
        sounds = [s for s in self.cfg["sounds"] if text in s["name"].lower()]
        if not sounds:
            empty = QLabel("Aún no hay sonidos. Añade archivos o descárgalos de internet."
                           if not text else "Sin resultados.")
            empty.setObjectName("status")
            self.grid.addWidget(empty, 0, 0)
        cols = self._grid_columns()
        self._grid_cols = cols
        self.sound_buttons = {}
        for i, s in enumerate(sounds):
            btn = SoundButton(("✂ " if s.get("start") or s.get("end") else "") + s["name"], s["hotkey"])
            btn.clicked.connect(lambda _=False, sid=s["id"]: self.play_sound(sid))
            btn.trim.clicked.connect(lambda _=False, sid=s["id"]: self._trim(self._entry(sid)))
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda _p, sid=s["id"], b=btn: self._sound_menu(sid, b))
            self.grid.addWidget(btn, i // cols, i % cols)
            self.sound_buttons[s["id"]] = btn

    def _grid_columns(self):
        width = self.grid_scroll.viewport().width() - 20
        return max(1, width // (SoundButton.W + self.grid.horizontalSpacing()))

    def _update_playing(self):
        """Ilumina los botones que están sonando y mueve su barra de progreso."""
        for sid, btn in self.sound_buttons.items():
            btn.set_progress(self.engine.progress(sid))

    def eventFilter(self, obj, e):
        # Al cambiar el ancho del panel de sonidos, recolocar los botones en las columnas que quepan
        if obj is self.grid_scroll.viewport() and e.type() == QEvent.Resize:
            if self._grid_columns() != getattr(self, "_grid_cols", None):
                QTimer.singleShot(0, self.rebuild_grid)
        return super().eventFilter(obj, e)

    def play_sound(self, sid):
        s = self._entry(sid)
        if not s:
            return
        try:
            norm = s.get("norm_gain", 1.0) if self.cfg["normalize"] else 1.0
            self.engine.play_file(SOUNDS_DIR / s["file"], s["volume"] * norm, tag=sid,
                                  start=s.get("start", 0.0), end=s.get("end"))
        except Exception as e:
            self.bridge.error.emit(f"No se pudo reproducir «{s['name']}»: {e}")

    def _sound_menu(self, sid, btn):
        s = self._entry(sid)
        menu = QMenu(self)
        acts = {
            "▶ Reproducir": lambda: self.play_sound(sid),
            "■ Detener este": lambda: [o.stop(sid) for o in (self.engine.cable, self.engine.monitor)],
            "✂ Recortar…": lambda: self._trim(s),
            "✎ Renombrar…": lambda: self._rename(s),
            "🔊 Volumen…": lambda: self._volume(s),
            "⌨ Asignar atajo…": lambda: self.capture_hotkey(sid),
            "⌫ Quitar atajo": lambda: self._set_hotkey(sid, ""),
            "🗑 Eliminar": lambda: self._delete(s),
        }
        for text, fn in acts.items():
            a = QAction(text, menu)
            a.triggered.connect(fn)
            menu.addAction(a)
        menu.exec(btn.mapToGlobal(btn.rect().center()))

    def _measure_loudness(self, entries):
        """Mide en segundo plano la sonoridad de cada sonido (del trozo recortado, si lo hay)."""
        def job():
            for s in entries:
                try:
                    data, sr = audio.load_audio(SOUNDS_DIR / s["file"])
                    if s.get("start") or s.get("end"):
                        data = audio.trim(data, sr, s.get("start", 0.0), s.get("end"))
                    s["norm_gain"] = round(audio.loudness_gain(data, sr), 4)
                    print(f"[volumen] {s['name'][:40]}: ajuste {20 * np.log10(s['norm_gain']):+.1f} dB", flush=True)
                except Exception as e:
                    print(f"[volumen] no se pudo medir {s['name']}: {e}", flush=True)
            self.cfg.save()
        if entries:
            threading.Thread(target=job, daemon=True).start()

    def _trim(self, s):
        if not s:
            return
        try:  # si acaba de sonar, ya está decodificado en memoria: abre al instante
            data, sr = self.engine.cached(SOUNDS_DIR / s["file"]) or audio.load_audio(SOUNDS_DIR / s["file"])
        except Exception as e:
            self.bridge.error.emit(f"No se pudo abrir «{s['name']}»: {e}")
            return
        if TrimDialog(self, s, data, sr).exec():
            self.cfg.save()
            self.rebuild_grid()
            self._measure_loudness([s])  # el trozo puede ser más fuerte o flojo que el tema entero

    def _rename(self, s):
        name, ok = QInputDialog.getText(self, "Renombrar", "Nombre:", text=s["name"])
        if ok and name.strip():
            s["name"] = name.strip()
            self.cfg.save()
            self.rebuild_grid()

    def _volume(self, s):
        v, ok = QInputDialog.getInt(self, "Volumen", f"Volumen de «{s['name']}» (%):",
                                    int(s["volume"] * 100), 0, 300, 5)
        if ok:
            s["volume"] = v / 100
            self.cfg.save()

    def _delete(self, s):
        if QMessageBox.question(self, "Eliminar", f"¿Eliminar «{s['name']}»?") != QMessageBox.Yes:
            return
        self.cfg["sounds"].remove(s)
        self.cfg.save()
        path = SOUNDS_DIR / s["file"]
        self.engine.forget(path)
        if not any(x["file"] == s["file"] for x in self.cfg["sounds"]):
            path.unlink(missing_ok=True)
        self.rebuild_grid()
        self.register_hotkeys()

    def _add_entry(self, entry):
        self.cfg["sounds"].append(entry)
        self._assign_fkeys()
        self.cfg.save()
        self.rebuild_grid()
        self.register_hotkeys()
        if "norm_gain" not in entry:
            self._measure_loudness([entry])
        self.bridge.log.emit(f"＋ Añadido «{entry['name']}»")

    def add_files(self, paths=None):
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Añadir sonidos", "",
                ";;".join([
                    "Audio y vídeo (" + " ".join(f"*{e}" for e in sorted(library.ALL_EXTS)) + ")",
                    "Audio (" + " ".join(f"*{e}" for e in sorted(library.AUDIO_EXTS)) + ")",
                    "Vídeo: se extrae el audio (" + " ".join(f"*{e}" for e in sorted(library.VIDEO_EXTS)) + ")",
                    "Todos los archivos (*)",
                ]))
        for p in paths:
            try:
                self._add_entry(library.import_file(p))
            except Exception as e:
                self.bridge.error.emit(f"«{Path(p).name}» no es un audio válido: {e}")

    def download_url(self):
        url, ok = QInputDialog.getText(
            self, "Descargar sonido",
            "Pega un enlace (MP3 directo, MyInstants, YouTube, SoundCloud…):")
        if not ok or not url.strip():
            return
        self._start_download(url.strip(), self.cfg["ytdlp_browser"])

    def _start_download(self, url, browser):
        b = self.bridge
        b.dl_progress.emit(-1, "Empezando…")

        def job():
            try:
                b.sound_added.emit(library.download(
                    url, lambda frac, text: b.dl_progress.emit(-1 if frac is None else int(frac * 1000), text),
                    browser))
                b.status.emit("Descarga completada ✔")
            except library.NeedsLogin as e:
                b.need_login.emit(url, str(e))
            except library.BrowserLocked:
                b.browser_locked.emit(url, browser)
            except Exception as e:
                b.dl_failed.emit(url, library.friendly_error(e))
            finally:
                b.dl_progress.emit(-2, "")
        threading.Thread(target=job, daemon=True).start()

    def _on_dl_progress(self, value, text):
        if value == -2:  # terminado
            self.dl_box.hide()
            return
        self.dl_box.show()
        if value < 0:
            self.dl_bar.setRange(0, 0)  # animación "trabajando"
        else:
            self.dl_bar.setRange(0, 1000)
            self.dl_bar.setValue(value)
            self.dl_bar.setFormat(f"{value / 10:.0f} %")
        self.dl_label.setText(text)

    def _on_need_login(self, url, reason):
        """YouTube pide sesión: preguntar con qué navegador (en el que tengas YouTube iniciado)."""
        found = library.installed_browsers()
        if not found:
            self.bridge.error.emit(f"{reason} No he encontrado ningún navegador compatible.")
            return
        browser, ok = QInputDialog.getItem(
            self, "Este vídeo necesita tu sesión de YouTube",
            f"{reason}\n\nGhost Voice puede usar la sesión de YouTube de tu navegador (solo para descargar, "
            "no se guarda ni se envía a ningún sitio).\nElige el navegador donde tienes YouTube iniciado "
            "con una cuenta mayor de edad:", found, 0, False)
        if ok:
            self._set("ytdlp_browser", browser)
            self._start_download(url, browser)

    def _on_browser_locked(self, url, browser):
        """El navegador abierto bloquea su sesión: pedir que se cierre y reintentar."""
        while True:
            running = library.browser_running(browser)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle(f"Cierra {browser} un momento")
            box.setText(f"<b>{browser} está abierto</b> y bloquea su sesión, así que no puedo usarla para "
                        "descargar este vídeo." if running else
                        f"{browser} parece cerrado. Pulsa Reintentar.")
            box.setInformativeText(
                f"1. Cierra {browser} del todo (revisa también el icono junto al reloj y elige «Salir»).\n"
                "2. Pulsa Reintentar. Cuando termine la descarga ya puedes volver a abrirlo.\n\n"
                + ("⚠ Todavía lo veo abierto." if running else ""))
            retry = box.addButton("Reintentar", QMessageBox.AcceptRole)
            other = box.addButton("Usar otro navegador", QMessageBox.ActionRole)
            box.addButton("Cancelar", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is other:
                self._set("ytdlp_browser", "")
                self._on_need_login(url, "Elige otro navegador.")
                return
            if box.clickedButton() is not retry:
                return
            if not library.browser_running(browser):
                self._start_download(url, browser)
                return

    def _on_dl_failed(self, url, reason):
        self.bridge.error.emit(f"No se pudo descargar: {reason}")
        QMessageBox.warning(self, "No se pudo descargar", f"{reason}\n\n{url}")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    # ================================================================= atajos

    def register_hotkeys(self):
        """Teclas sueltas (F1, F2…) van por un gancho propio que respeta la capa de Bloq Mayús;
        las combinaciones (ctrl+x…) usan add_hotkey y funcionan siempre."""
        keyboard.unhook_all()
        b = self.bridge
        keymap = {}
        bindings = [(s["hotkey"], ("sound", s["id"])) for s in self.cfg["sounds"] if s["hotkey"]]
        bindings += [(self.cfg["stop_hotkey"], ("stop", None)), (self.cfg["ptt_key"], ("ptt", None))]
        for hk, action in bindings:
            if not hk:
                continue
            if "+" in hk and hk != "+":
                try:
                    keyboard.add_hotkey(hk, b.play.emit if action[0] == "sound" else b.stop.emit,
                                        args=(action[1],) if action[0] == "sound" else ())
                except Exception as e:
                    b.error.emit(f"Atajo inválido «{hk}»: {e}")
            else:
                keymap[hk.lower()] = action
        self._keymap = keymap
        self._held = set()
        self._last_logged = None
        keyboard.hook(self._on_key, suppress=True)
        print(f"[atajos] activos: {sorted(keymap)} · capa Bloq Mayús: {self.cfg['caps_layer']}", flush=True)

    def _on_key(self, event):
        """Se ejecuta en el hilo del gancho de teclado: debe ser rapidísimo.
        Devuelve False para que la tecla no llegue al juego."""
        name = (event.name or "").lower()
        if self._ptt_down and event.event_type == keyboard.KEY_DOWN and name != self.cfg["ptt_key"].lower():
            # Otra tecla (p. ej. WASD) corta la autorrepetición de F1: la red de seguridad ya no es fiable
            self._ptt_watchdog_armed = False
        # Diagnóstico: solo teclas especiales (F1, Bloq Mayús…), nunca letras ni números
        if event.event_type == keyboard.KEY_DOWN and len(name) > 1 and name != self._last_logged:
            self._last_logged = name
            print(f"[tecla] {event.name!r} scan={event.scan_code} bloq_mayus={int(caps_lock_on())}", flush=True)
        elif event.event_type == keyboard.KEY_UP and name == self._last_logged:
            self._last_logged = None
        action = self._keymap.get(name)
        if action is None:
            return True
        layer = self.cfg["caps_layer"]
        down = event.event_type == keyboard.KEY_DOWN
        if down:
            if name not in self._held:
                caps = caps_lock_on()
                self.bridge.status.emit(
                    f"Tecla {name.upper()} detectada — " + ("panel activo ✔" if caps or not layer
                                                           else "Bloq Mayús apagado: va al juego"))
                if layer and not caps:
                    return True
            repeat = name in self._held
            self._held.add(name)
        else:
            if name not in self._held:
                return True  # la pulsación no era nuestra
            self._held.discard(name)
            repeat = False
        kind, arg = action
        if kind == "ptt":
            if down:
                self._ptt_last_event = time.monotonic()  # también las repeticiones del teclado
            if not repeat:
                self.bridge.ptt.emit(down)
        elif down and not repeat:
            self.bridge.play.emit(arg) if kind == "sound" else self.bridge.stop.emit()
        return not layer

    # ------------------------------------------- pulsar para hablar automático

    def _apply_auto_ptt(self):
        on = self.cfg["auto_ptt_enabled"] and bool(self.cfg["auto_ptt_key"])
        self.engine.preroll = self.cfg["preroll_ms"] / 1000 if on else 0.0
        if not on:
            self._release_auto_ptt()

    def _ptt_watchdog(self):
        """Si se pierde el evento de soltar F1, lo detectamos porque dejan de llegar las repeticiones
        automáticas del teclado (unas 30 por segundo mientras se mantiene pulsada)."""
        if not (self._ptt_down and self._ptt_by_key):
            return
        now = time.monotonic()
        if self._ptt_watchdog_armed and now - self._ptt_last_event > REPEAT_DELAY + 0.35:
            reason = "soltar perdido: se cierra por falta de repeticiones del teclado"
        elif now - self._ptt_down_at > 30:
            reason = "grabación de más de 30 s: se cierra por seguridad"
        else:
            return
        print(f"[ptt] {reason}", flush=True)
        self._held.discard(self.cfg["ptt_key"].lower())
        self._on_ptt(False)

    def _caps_watch(self):
        """Al pasar Bloq Mayús de encendido a apagado, silencio total."""
        on = caps_lock_on()
        if on != self._caps_prev and self.cfg["caps_layer"]:
            if not on and self.cfg["caps_off_stops"]:
                self.cut_all()
            if self.cfg["caps_beep"]:  # después del corte, para que el corte no se lo coma
                self.engine.cue(*((660, 990) if on else (990, 660)))
        self._caps_prev = on

    def cut_all(self):
        """Corta lo que suena, descarta las frases pendientes y suelta la tecla de hablar."""
        self._generation += 1  # lo que se esté generando ahora ya no sonará
        keep = []
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            if job[0] == "save":  # guardar como sonido no suena: no se pierde
                keep.append(job)
        for job in keep:
            self.jobs.put(job)
        self.engine.stop_all()
        self._release_auto_ptt()
        print("[corte] Bloq Mayús apagado: silencio", flush=True)
        self.bridge.status.emit("⏹ Cortado (Bloq Mayús apagado)")

    def _auto_ptt_tick(self):
        """Mantiene la tecla de hablar de Discord/juego mientras el cable tenga audio, +0,4 s de cola."""
        self._caps_watch()
        self._ptt_watchdog()
        if not (self.cfg["auto_ptt_enabled"] and self.cfg["auto_ptt_key"]):
            return
        now = time.monotonic()
        if self.engine.cable.busy:
            self._auto_ptt_last = now
            if self._auto_ptt_down is None:
                try:
                    keyboard.press(self.cfg["auto_ptt_key"])
                    self._auto_ptt_down = self.cfg["auto_ptt_key"]
                    print(f"[auto-ptt] pulsa {self._auto_ptt_down}", flush=True)
                except Exception as e:
                    self.bridge.error.emit(f"No se pudo pulsar «{self.cfg['auto_ptt_key']}»: {e}")
                    self._set("auto_ptt_enabled", False)
        elif self._auto_ptt_down is not None and now - self._auto_ptt_last > 0.4:
            self._release_auto_ptt()

    def _release_auto_ptt(self):
        if self._auto_ptt_down is not None:
            try:
                keyboard.release(self._auto_ptt_down)
            finally:
                print(f"[auto-ptt] suelta {self._auto_ptt_down}", flush=True)
                self._auto_ptt_down = None

    def _assign_fkeys(self):
        """Da F2-F9 a los sonidos que no tengan atajo, en orden."""
        used = {s["hotkey"] for s in self.cfg["sounds"]}
        free = [k for k in SOUND_FKEYS if k not in used]
        changed = False
        for s in self.cfg["sounds"]:
            if not s["hotkey"] and free:
                s["hotkey"] = free.pop(0)
                changed = True
        if changed:
            self.cfg.save()

    def _update_layer_label(self):
        if not self.cfg["caps_layer"]:
            self.layer_label.setText("⌨ Atajos siempre activos")
            self.layer_label.setStyleSheet("color:#9a9aa8")
        elif caps_lock_on():
            self.layer_label.setText("🔒 BLOQ MAYÚS: panel ACTIVO — F1 habla · F2–F9 sonidos")
            self.layer_label.setStyleSheet("color:#7dffa8; font-weight:700")
        else:
            self.layer_label.setText("🔓 Bloq Mayús apagado — las teclas F van al juego")
            self.layer_label.setStyleSheet("color:#9a9aa8")

    def capture_hotkey(self, target, single=False):
        """target: id de sonido, 'stop' o 'ptt'. Escucha la siguiente tecla/combinación pulsada."""
        keyboard.unhook_all()
        self.bridge.status.emit("Pulsa la tecla o combinación que quieras usar…")

        def job():
            if single:
                while (ev := keyboard.read_event()).event_type != keyboard.KEY_DOWN:
                    pass
                hk = ev.name
            else:
                hk = keyboard.read_hotkey(suppress=False)
            self.bridge.hotkey_captured.emit(target, hk)
        self._run(job)

    def _on_hotkey_captured(self, target, hk):
        if hk == "esc":
            hk = ""
        if target == "ptt":
            self._set("ptt_key", hk)
            self.ptt_key_btn.setText(hk or "(ninguna)")
        elif target == "auto_ptt":
            self._release_auto_ptt()
            self._set("auto_ptt_key", hk)
            self.auto_ptt_btn.setText(hk or "(elegir tecla)")
            self._apply_auto_ptt()
        elif target == "stop":
            self._set("stop_hotkey", hk)
            self.stop_key_btn.setText(hk or "(ninguno)")
        else:
            self._set_hotkey(target, hk)
        self.bridge.status.emit(f"Atajo asignado: {hk or 'ninguno'}")
        self.register_hotkeys()

    def _set_hotkey(self, sid, hk):
        s = self._entry(sid)
        if s:
            s["hotkey"] = hk
            self.cfg.save()
            self.rebuild_grid()
            self.register_hotkeys()

    # =================================================================== voz

    def load_stt_model(self):
        name = self.cfg["stt_model"]
        self.bridge.status.emit(f"Cargando modelo Whisper «{name}» (la primera vez se descarga)…")

        def job():
            t = time.monotonic()
            dev = self.stt.load(name)
            print(f"[whisper] {name} en {dev} (cargado en {time.monotonic() - t:.1f} s)", flush=True)
            self.bridge.status.emit(f"Reconocimiento de voz listo: {name} en {dev}")
        self._run(job)

    def _keep_warm(self):
        if not self._ptt_down and self.jobs.empty():
            threading.Thread(target=self.stt.keep_warm, daemon=True).start()

    def _on_ptt_key(self, down):
        if down and not self._ptt_down:
            self._ptt_by_key = True
            self._ptt_watchdog_armed = True
            self._ptt_last_event = time.monotonic()
        self._on_ptt(down)

    def _on_ptt(self, down):
        if down == self._ptt_down:
            return  # ignora la autorrepetición del teclado
        self._ptt_down = down
        if down:
            self._ptt_down_at = time.monotonic()
            print("[ptt] pulsado", flush=True)
        else:
            self._ptt_by_key = False
            print(f"[ptt] soltado tras {time.monotonic() - self._ptt_down_at:.1f} s", flush=True)
        self.ptt_btn.setProperty("live", down)
        self.ptt_btn.style().polish(self.ptt_btn)
        self.engine.cue(880 if down else 600)
        if down:
            try:
                self.recorder.start(audio.find_device(self.cfg["mic_device"], "input"))
                self.ptt_btn.setText("🔴 Escuchando… suelta para enviar")
                self.bridge.status.emit("🔴 Escuchando…")
            except Exception as e:
                self.bridge.error.emit(f"No se pudo abrir el micrófono: {e}")
        else:
            self.ptt_btn.setText("Mantén pulsado para hablar")
            t = time.monotonic()
            clip = self.recorder.stop()
            print(f"[tiempos] grabado {len(clip) / 16000:.1f} s de audio (parar micro: "
                  f"{time.monotonic() - t:.2f} s)", flush=True)
            if len(clip) > 16000 * 0.3:
                self.jobs.put(("audio", (clip, time.monotonic())))

    def say_typed(self):
        text = self.say_edit.text().strip()
        if text:
            self.speak(text)
            self.say_edit.clear()

    def speak(self, text):
        self.jobs.put(("text", (text, time.monotonic())))

    def save_text_as_sound(self, text=None):
        """Genera la frase con la voz, efecto e idioma actuales y la añade al panel de sonidos."""
        text = (text or self.say_edit.text()).strip()
        if text:
            self.bridge.status.emit("Generando sonido…")
            self.jobs.put(("save", (text, time.monotonic())))

    def save_last_as_sound(self):
        if self._last_tts is None:
            self.bridge.status.emit("Aún no se ha dicho ninguna frase")
            return
        data, sr, text = self._last_tts
        self._add_entry(library.save_generated(data, sr, text, self.cfg["tts_volume"]))

    def _test_voice(self):
        src, _dst, _label = speech.MODES[self.cfg["voice_mode"]]
        self.speak("Hello there, I am ready to play." if src == "en" else "Hola, estoy listo para jugar.")

    def save_phrase(self):
        text = self.say_edit.text().strip()
        if text and text not in self.cfg["phrases"]:
            self.cfg["phrases"].append(text)
            self.cfg.save()
            self.phrases.addItem(text)

    def _phrase_menu(self, pos):
        item = self.phrases.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        as_sound = menu.addAction("🔊 Guardar como sonido del panel")
        delete = menu.addAction("🗑 Eliminar frase")
        chosen = menu.exec(self.phrases.mapToGlobal(pos))
        if chosen == as_sound:
            self.save_text_as_sound(item.text())
        elif chosen == delete:
            self.cfg["phrases"].remove(item.text())
            self.cfg.save()
            self.phrases.takeItem(self.phrases.row(item))

    def _voice_worker(self):
        """Procesa en orden: grabación -> texto -> voz sintética -> micrófono virtual."""
        while True:
            kind, (payload, t0) = self.jobs.get()
            generation = self._generation
            times = [f"cola {time.monotonic() - t0:.2f}"]

            def lap(label, start):
                times.append(f"{label} {time.monotonic() - start:.2f}")
                return time.monotonic()
            try:
                src, dst, _label = speech.MODES[self.cfg["voice_mode"]]
                t = time.monotonic()
                if kind == "audio":
                    self.bridge.status.emit("Transcribiendo…")
                    text = self.stt.transcribe(payload, src)
                    t = lap("whisper", t)
                    if not text:
                        self.bridge.status.emit("No se ha entendido nada")
                        continue
                    self.bridge.log.emit(f"🎙 Has dicho: <b>{escape(text)}</b>")
                else:
                    text = payload
                    if kind == "text":
                        self.bridge.log.emit(f"⌨ Escrito: <b>{escape(text)}</b>")
                if src != dst:
                    self.bridge.status.emit("Traduciendo…")
                    text = self.translator.translate(text, src, dst)
                    t = lap("traducir", t)
                    self.bridge.log.emit(f"&nbsp;&nbsp;🌐 → <b>{escape(text)}</b>")
                self.bridge.status.emit("Sintetizando voz…")
                rate, pitch = self.cfg["tts_rate"], self.cfg["tts_pitch"]
                engine = self.cfg["tts_engine"]
                voice = self.cfg.get(f"{engine}_voice_{dst}", "")
                if not voice:
                    raise RuntimeError(f"No hay voz en {'inglés' if dst == 'en' else 'español'} para este motor. "
                                       "Elige otro motor o instala voces en Windows (Configuración > Hora e "
                                       "idioma > Voz).")
                data, sr = speech.ENGINES[engine][2](text, voice, rate, pitch)
                t = lap(f"voz({engine})", t)
                data, sr = effects.apply(self.cfg["voice_effect"], data, sr)
                if self.cfg["normalize"]:  # misma sonoridad que los sonidos del panel
                    data = data * audio.loudness_gain(data, sr)
                t = lap("efecto", t)
                if kind == "save":  # se guarda en el panel en vez de sonar
                    self.bridge.sound_added.emit(library.save_generated(data, sr, text, self.cfg["tts_volume"]))
                    self.bridge.status.emit("Guardada como sonido ✔")
                    continue
                self._last_tts = (data, sr, text)
                self.engine.wait_until_free("tts")  # no pisar la frase anterior
                t = lap("esperar frase anterior", t)
                if generation != self._generation:
                    print(f"[corte] descartada «{text[:40]}»", flush=True)
                    continue
                self.engine.play_array(data, sr, self.cfg["tts_volume"], tag="tts")
                lap("preparar audio", t)
                times.append(f"TOTAL hasta que suena {time.monotonic() - t0 + self.engine.preroll:.2f}")
                print(f"[tiempos] {kind}: " + " · ".join(times) + f" s · «{text[:40]}»", flush=True)
                self.bridge.status.emit("Enviado ✔")
            except Exception as e:
                traceback.print_exc()
                self.bridge.error.emit(str(e))

    def _open_blend_dialog(self):
        try:
            base = speech.kokoro_base_voices()
        except Exception as e:
            self.bridge.error.emit(f"Kokoro no está disponible: {e}")
            return
        BlendDialog(self, base).exec()
        self._refresh_my_voices()
        if self.cfg["tts_engine"] == "kokoro":
            self._load_voices()

    # ------------------------------------------------------------ mis voces

    def _refresh_my_voices(self):
        """Todas las voces guardadas (mezclas y clonadas), estén en el motor que estén."""
        self.my_voices.clear()
        mine = [("kokoro", "★", n, "mezcla") for n in self.cfg["kokoro_blends"]]
        mine += [("clon", "🧬", n, "clonada") for n in clone.voice_names()]
        if not mine:
            self.my_voices.addItem("(aún no tienes: crea una mezcla o clona una voz abajo)", None)
            return
        self.my_voices.addItem("— elige una de tus voces —", None)
        for engine, mark, name, kind in mine:
            self.my_voices.addItem(f"{mark} {name}  ({kind})", (engine, mark, name))
        current = self.cfg["tts_engine"], self.cfg.get(f"{self.cfg['tts_engine']}_voice_en", "")
        for i in range(1, self.my_voices.count()):
            engine, mark, name = self.my_voices.itemData(i)
            if current == (engine, f"en-us · {mark} {name}"):
                self.my_voices.setCurrentIndex(i)

    def use_my_voice(self, data):
        """Pone esa voz en inglés y en español, y cambia al motor que la usa."""
        if not data:
            return
        engine, mark, name = data
        self.cfg[f"{engine}_voice_en"] = f"en-us · {mark} {name}"
        self.cfg[f"{engine}_voice_es"] = f"es-es · {mark} {name}"
        self.cfg.save()
        index = list(speech.ENGINES).index(engine)
        if self.engine_box.currentIndex() != index:
            self.engine_box.setCurrentIndex(index)  # recarga las voces y selecciona la nuestra
        else:
            self._load_voices()
        self._refresh_my_voices()
        self.bridge.status.emit(f"Voz activa: {mark} {name}")

    def _load_voices(self):
        engine = self.cfg["tts_engine"]

        def job():
            self.bridge.voices.emit(engine, speech.ENGINES[engine][1]())
        self._run(job)

    def _fill_voices(self, engine, names):
        if engine != self.cfg["tts_engine"]:
            return
        for lang, box in self.voice_boxes.items():
            key = f"{engine}_voice_{lang}"
            items = [n for n in names if speech.voice_language(n) == lang]
            current = self.cfg.get(key, "")
            box.blockSignals(True)
            box.clear()
            box.addItems(items)
            box.blockSignals(False)
            if current not in items:
                current = items[0] if items else ""
                self._set(key, current)
            box.setCurrentText(current)
            box.setToolTip(f"{len(items)} voces disponibles")

    def _warmup_clone(self):
        """Arranca el motor de clonación en segundo plano para que la primera frase no tarde ~40 s."""
        if self.cfg["tts_engine"] != "clon" or not clone.available():
            return
        names = clone.voice_names()
        if not names:
            return

        def job():
            self.bridge.status.emit("Preparando el motor de clonación (la primera vez ~1 min)…")
            clone.warmup(names[0])
            self.bridge.status.emit("Motor de clonación listo ✔")
        self._run(job)

    def _engine_changed(self):
        self._set("tts_engine", self.engine_box.currentData())
        self._warmup_clone()
        for box in self.voice_boxes.values():
            box.clear()
        self._load_voices()

    def _voice_changed(self, lang, name):
        if name:
            self._set(f"{self.cfg['tts_engine']}_voice_{lang}", name)

    def _mode_changed(self):
        self._set("voice_mode", self.mode_box.currentData())
        self._preload_translator()

    def _preload_translator(self):
        src, dst, label = speech.MODES[self.cfg["voice_mode"]]
        if src == dst:
            return

        def job():
            self.bridge.status.emit("Preparando traductor (la primera vez se descarga)…")
            self.translator.preload(src, dst)
            self.bridge.status.emit(f"Traductor listo: {label}")
        self._run(job)

    def closeEvent(self, e):
        self._release_auto_ptt()
        clone.server.stop()
        keyboard.unhook_all()
        self.engine.cable.close()
        self.engine.monitor.close()
        super().closeEvent(e)


class SoundButton(QPushButton):
    """Botón del panel de tamaño fijo: nombre en 2 líneas como mucho, tecla abajo y,
    mientras suena, borde verde con barra de progreso."""
    W, H = 190, 76

    def __init__(self, name, hotkey):
        super().__init__()
        self.setObjectName("sound")
        self.setFixedSize(self.W, self.H)
        self.setToolTip(f"{name}\nClic = reproducir · ✂ = recortar · clic derecho = más opciones")
        self.name, self.hotkey = name, hotkey
        self.progress = None
        self.trim = QToolButton(self)  # cortador siempre a la vista, en la esquina
        self.trim.setObjectName("trim")
        self.trim.setText("✂")
        self.trim.setToolTip("Recortar este sonido")
        self.trim.setCursor(Qt.PointingHandCursor)
        self.trim.setGeometry(self.W - 26, 4, 22, 22)

    def set_progress(self, value):
        if value != self.progress:
            playing_changed = (value is None) != (self.progress is None)
            self.progress = value
            if playing_changed:
                self.setProperty("playing", value is not None)
                self.style().polish(self)
            self.update()

    def _lines(self, fm, width):
        words, lines, current = self.name.split(), [], ""
        for word in words:
            test = f"{current} {word}".strip()
            if fm.horizontalAdvance(test) <= width or not current:
                current = test
            else:
                lines.append(current)
                current = word
        lines.append(current)
        if len(lines) > 2:
            lines = [lines[0], " ".join(lines[1:])]
        return [fm.elidedText(line, Qt.ElideRight, width) for line in lines]

    def paintEvent(self, e):
        super().paintEvent(e)  # fondo y borde según el estilo
        p = QPainter(self)
        bold, normal = QFont(p.font()), QFont(p.font())
        bold.setBold(True)
        normal.setBold(False)
        p.setFont(bold)  # medir con la misma letra (negrita) con la que se dibuja
        fm = p.fontMetrics()
        text_w = self.W - 2 * 28  # deja sitio al ✂ de la esquina
        lines = self._lines(fm, text_w)
        if self.hotkey:
            lines.append(f"[{self.hotkey}]")
        line_h = fm.height()
        y = (self.H - line_h * len(lines)) // 2
        for i, line in enumerate(lines):
            is_key = self.hotkey and i == len(lines) - 1
            p.setPen(QColor("#a993ff" if is_key else "#e6e6eb"))
            p.setFont(normal if is_key else bold)
            p.drawText(QRect(28, y + i * line_h, text_w, line_h), Qt.AlignCenter, line)
        if self.progress is not None:
            p.fillRect(8, self.H - 9, self.W - 16, 4, QColor("#2c2c35"))
            p.fillRect(8, self.H - 9, int((self.W - 16) * self.progress), 4, QColor("#7dffa8"))
        p.end()


class WaveTrim(QWidget):
    """Forma de onda con dos tiradores (inicio y fin) que se arrastran con el ratón."""
    changed = Signal(float, float)
    GRAB = 10  # píxeles de margen para agarrar un tirador

    RES = 400  # picos por segundo: suficiente detalle para hacer mucho zoom

    def __init__(self, data, sr):
        super().__init__()
        self._drag = None
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.load(data, sr)

    def load(self, data, sr):
        mono = np.abs(data).max(axis=1) if data.ndim == 2 else np.abs(data)
        self.duration = max(len(mono) / sr, 1e-3)
        step = max(1, sr // self.RES)
        n = len(mono) // step
        self.peaks = mono[: n * step].reshape(n, step).max(axis=1) if n else np.zeros(1)
        self.peaks = self.peaks / (float(self.peaks.max()) or 1.0)
        self.pps = len(self.peaks) / self.duration  # picos por segundo reales
        self.start, self.end = 0.0, self.duration
        self.view = (0.0, self.duration)  # trozo visible (zoom)
        self.playhead = None
        self.update()

    def _x(self, t):
        vs, ve = self.view
        return int((t - vs) / (ve - vs) * (self.width() - 1))

    def _t(self, x):
        vs, ve = self.view
        return min(self.duration, max(0.0, vs + x / max(1, self.width() - 1) * (ve - vs)))

    def set_view(self, vs, ve):
        span = min(self.duration, max(0.2, ve - vs))
        vs = min(max(0.0, vs), self.duration - span)
        self.view = (vs, vs + span)
        self.update()

    def zoom_selection(self):
        margin = (self.end - self.start) * 0.15 + 0.05
        self.set_view(self.start - margin, self.end + margin)

    def wheelEvent(self, e):
        """Rueda del ratón: zoom centrado en el cursor."""
        t = self._t(e.position().x())
        factor = 0.8 if e.angleDelta().y() > 0 else 1.25
        vs, ve = self.view
        self.set_view(t - (t - vs) * factor, t + (ve - t) * factor)

    def set_range(self, start, end):
        min_len = min(0.05, self.duration)
        self.start = max(0.0, min(start, self.duration - min_len))
        self.end = min(self.duration, max(end, self.start + min_len))
        self.update()
        self.changed.emit(self.start, self.end)

    def paintEvent(self, _e):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#1c1c24"))
        xs, xe = self._x(self.start), self._x(self.end)
        p.fillRect(xs, 0, xe - xs, h, QColor("#2b2050"))
        mid = h / 2
        last = len(self.peaks) - 1
        for x in range(w):
            a = min(last, int(self._t(x) * self.pps))
            b = min(last + 1, max(a + 1, int(self._t(x + 1) * self.pps)))
            amp = float(self.peaks[a:b].max()) * (h / 2 - 6)
            p.setPen(QColor("#a993ff") if xs <= x <= xe else QColor("#4a4a58"))
            p.drawLine(x, int(mid - amp), x, int(mid + amp))
        for x in (xs, xe):  # tiradores (solo si están dentro de la vista)
            if -6 <= x <= w + 6:
                p.fillRect(x - 2, 0, 4, h, QColor("#ffffff"))
                p.fillRect(x - 6, int(mid) - 14, 12, 28, QColor("#6d4aff"))
        if self.view != (0.0, self.duration):
            p.setPen(QColor("#9a9aa8"))
            p.drawText(6, 14, f"🔍 {self.view[0]:.2f} – {self.view[1]:.2f} s")
        if self.playhead is not None:
            p.setPen(QPen(QColor("#7dffa8"), 2))
            x = self._x(self.playhead)
            p.drawLine(x, 0, x, h)
        p.end()

    def mousePressEvent(self, e):
        x = e.position().x()
        ds, de = abs(x - self._x(self.start)), abs(x - self._x(self.end))
        self._drag = "start" if ds <= de else "end"  # el tirador más cercano
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e):
        x = e.position().x()
        near = min(abs(x - self._x(self.start)), abs(x - self._x(self.end))) <= self.GRAB
        self.setCursor(Qt.SizeHorCursor if near or self._drag else Qt.ArrowCursor)
        if self._drag == "start":
            self.set_range(self._t(x), self.end)
        elif self._drag == "end":
            self.set_range(self.start, self._t(x))

    def mouseReleaseEvent(self, _e):
        self._drag = None


class TrimDialog(QDialog):
    """Recorte no destructivo: guarda inicio y fin en el sonido, el archivo no se modifica."""

    def __init__(self, win, entry, data, sr):
        super().__init__(win)
        self.win, self.entry, self.data, self.sr = win, entry, data, sr
        self.setWindowTitle(f"Recortar «{entry['name']}»")
        self.setMinimumWidth(720)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Arrastra los tiradores morados para elegir el trozo que quieres conservar. "
                             "Rueda del ratón = zoom."))
        self.wave = WaveTrim(data, sr)
        lay.addWidget(self.wave)
        zoom_row = QHBoxLayout()
        zoom_in = QPushButton("🔍 Ampliar selección")
        zoom_in.clicked.connect(self.wave.zoom_selection)
        zoom_all = QPushButton("Ver todo")
        zoom_all.clicked.connect(lambda: self.wave.set_view(0.0, self.wave.duration))
        zoom_row.addWidget(zoom_in)
        zoom_row.addWidget(zoom_all)
        zoom_row.addStretch()
        lay.addLayout(zoom_row)
        row = QHBoxLayout()
        self.start_box, self.end_box = QDoubleSpinBox(), QDoubleSpinBox()
        for box in (self.start_box, self.end_box):
            box.setDecimals(3)
            box.setSingleStep(0.05)
            box.setSuffix(" s")
            box.setRange(0.0, self.wave.duration)
        self.start_box.valueChanged.connect(lambda v: self.wave.set_range(v, self.wave.end))
        self.end_box.valueChanged.connect(lambda v: self.wave.set_range(self.wave.start, v))
        self.info = QLabel()
        row.addWidget(QLabel("Inicio:"))
        row.addWidget(self.start_box)
        row.addWidget(QLabel("Fin:"))
        row.addWidget(self.end_box)
        row.addStretch()
        row.addWidget(self.info)
        lay.addLayout(row)
        buttons = QHBoxLayout()
        play = QPushButton("▶ Escuchar recorte (solo tú)")
        play.clicked.connect(self._preview)
        stop = QPushButton("■ Parar")
        stop.clicked.connect(self._stop)
        reset = QPushButton("↺ Restablecer")
        reset.clicked.connect(lambda: self.wave.set_range(0.0, self.wave.duration))
        save = QPushButton("💾 Guardar recorte")
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        for b in (play, stop, reset):
            buttons.addWidget(b)
        buttons.addStretch()
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        lay.addLayout(buttons)
        self.wave.changed.connect(self._sync)
        self.wave.set_range(entry.get("start", 0.0), entry.get("end") or self.wave.duration)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._t0 = None

    def _sync(self, start, end):
        for box, v in ((self.start_box, start), (self.end_box, end)):
            box.blockSignals(True)
            box.setValue(v)
            box.blockSignals(False)
        self.info.setText(f"Duración: <b>{end - start:.2f} s</b> de {self.wave.duration:.2f} s")

    def _preview(self):
        seg = audio.trim(self.data, self.sr, self.wave.start, self.wave.end)
        self.win.engine.preview(seg * self.entry.get("volume", 1.0), self.sr)
        self._t0 = time.monotonic()
        self._timer.start(30)

    def _tick(self):
        pos = self.wave.start + time.monotonic() - self._t0
        if pos >= self.wave.end:
            self._stop()
            return
        self.wave.playhead = pos
        self.wave.update()

    def _stop(self):
        self.win.engine.monitor.stop("preview")
        self._timer.stop()
        self.wave.playhead = None
        self.wave.update()

    def _save(self):
        self._stop()
        full = self.wave.start < 0.001 and self.wave.end > self.wave.duration - 0.001
        self.entry["start"] = 0.0 if full else round(self.wave.start, 3)
        self.entry["end"] = None if full else round(self.wave.end, 3)
        self.accept()

    def reject(self):
        self._stop()
        super().reject()


class BlendDialog(QDialog):
    """Mezcla hasta 4 voces Kokoro con porcentajes y la guarda con un nombre."""
    ROWS = 4

    def __init__(self, win, base_voices):
        super().__init__(win)
        self.win, self.cfg = win, win.cfg
        self.setWindowTitle("Mezclas de voces")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        top = QFormLayout()
        self.existing = QComboBox()
        self.existing.addItem("➕ Nueva mezcla", None)
        for name in self.cfg["kokoro_blends"]:
            self.existing.addItem(name, name)
        self.existing.currentIndexChanged.connect(self._load)
        top.addRow("Mezcla:", self.existing)
        self.name = QLineEdit(placeholderText="Nombre de tu voz, p. ej. «Capitán Ronco»")
        top.addRow("Nombre:", self.name)
        lay.addLayout(top)
        lay.addWidget(QLabel("Elige voces y cuánto pesa cada una (se reparte en proporción):"))
        grid = QGridLayout()
        self.rows = []
        for i in range(self.ROWS):
            box = QComboBox()
            box.addItem("(ninguna)", None)
            for v in base_voices:
                box.addItem(speech.kokoro_label(v), v)
            weight = _slider(0, 100, 50)
            pct = QLabel("50 %")
            weight.valueChanged.connect(lambda v, l=pct: l.setText(f"{v} %"))
            grid.addWidget(box, i, 0)
            grid.addWidget(weight, i, 1)
            grid.addWidget(pct, i, 2)
            self.rows.append((box, weight))
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        row = QHBoxLayout()
        self.text = QLineEdit("Hello, this is my new voice. Hola, esta es mi nueva voz.")
        self.lang = QComboBox()
        self.lang.addItem("Inglés", "en-us")
        self.lang.addItem("Español", "es-es")
        listen = QPushButton("🎧 Escuchar (solo tú)")
        listen.clicked.connect(self._preview)
        row.addWidget(self.text, 1)
        row.addWidget(self.lang)
        row.addWidget(listen)
        lay.addLayout(row)
        buttons = QHBoxLayout()
        delete = QPushButton("🗑 Borrar mezcla")
        delete.clicked.connect(self._delete)
        save = QPushButton("💾 Guardar")
        save.clicked.connect(self._save)
        use = QPushButton("✔ Guardar y usar esta voz")
        use.clicked.connect(self._save_and_use)
        close = QPushButton("Cerrar")
        close.clicked.connect(self.accept)
        buttons.addWidget(delete)
        buttons.addStretch()
        buttons.addWidget(save)
        buttons.addWidget(use)
        buttons.addWidget(close)
        lay.addLayout(buttons)
        self.rows[0][0].setCurrentIndex(self.rows[0][0].findData("am_michael"))
        self.rows[1][0].setCurrentIndex(self.rows[1][0].findData("bm_george"))

    def _parts(self):
        return [[box.currentData(), weight.value()] for box, weight in self.rows
                if box.currentData() and weight.value() > 0]

    def _load(self):
        name = self.existing.currentData()
        parts = self.cfg["kokoro_blends"].get(name, [])
        self.name.setText(name or "")
        for i, (box, weight) in enumerate(self.rows):
            voice, w = parts[i] if i < len(parts) else (None, 50)
            box.setCurrentIndex(max(0, box.findData(voice)))
            weight.setValue(int(w))

    def _preview(self):
        parts = self._parts()
        if not parts:
            return
        text, lang = self.text.text().strip(), self.lang.currentData()

        def job():
            self.win.engine.preview(*speech.kokoro_blend_synth(text, parts, lang))
        self.win._run(job)

    def _save(self):
        name, parts = self.name.text().strip(), self._parts()
        if not name or not parts:
            QMessageBox.warning(self, "Mezcla", "Ponle un nombre y elige al menos una voz.")
            return
        old = self.existing.currentData()
        if old and old != name:
            self.cfg["kokoro_blends"].pop(old, None)
        self.cfg["kokoro_blends"][name] = parts
        self.cfg.save()
        if self.existing.findData(name) < 0:
            self.existing.addItem(name, name)
        self.existing.setCurrentIndex(self.existing.findData(name))
        self.win.bridge.status.emit(f"Mezcla «{name}» guardada: está en ⭐ Mis voces")
        return name

    def _save_and_use(self):
        name = self._save()
        if name:
            self.win.use_my_voice(("kokoro", "★", name))
            self.accept()

    def _delete(self):
        name = self.existing.currentData()
        if name and QMessageBox.question(self, "Borrar", f"¿Borrar la mezcla «{name}»?") == QMessageBox.Yes:
            self.cfg["kokoro_blends"].pop(name, None)
            self.cfg.save()
            self.existing.removeItem(self.existing.currentIndex())


class CloneDialog(QDialog):
    """Gestiona las voces clonadas: añadir desde archivo o micro, escuchar, borrar y ajustes."""

    def __init__(self, win):
        super().__init__(win)
        self.win, self.cfg = win, win.cfg
        self.setWindowTitle("Voces clonadas")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        if not clone.available():
            lay.addWidget(QLabel("⚠ Falta el motor de clonación (carpeta clonador\\.venv)."))
        warn = QLabel("Usa tu propia voz, voces de personajes/audios que puedas usar, o de gente que te "
                      "dé permiso. No imites a personas reales sin su consentimiento.")
        warn.setWordWrap(True)
        warn.setObjectName("status")
        lay.addWidget(warn)

        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in (("🎧 Muestra original", self._play_ref), ("🗣 Probar clon", self._test),
                         ("✔ Usar esta voz", self._use), ("🗑 Borrar", self._delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)

        g = QGroupBox("Añadir una voz")
        f = QFormLayout(g)
        self.name = QLineEdit(placeholderText="Nombre, p. ej. «Narrador épico»")
        f.addRow("Nombre:", self.name)
        self.start, self.end = QLineEdit("0"), QLineEdit("")
        self.start.setPlaceholderText("segundo inicial")
        self.end.setPlaceholderText("segundo final (vacío = +20 s)")
        r = QHBoxLayout()
        r.addWidget(QLabel("Desde"))
        r.addWidget(self.start)
        r.addWidget(QLabel("hasta"))
        r.addWidget(self.end)
        f.addRow("Trozo (s):", r)
        r = QHBoxLayout()
        from_file = QPushButton("📂 Desde archivo (audio o vídeo)…")
        from_file.clicked.connect(self._from_file)
        self.rec_btn = QPushButton("🎙 Grabar con mi micro")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._record)
        r.addWidget(from_file)
        r.addWidget(self.rec_btn)
        f.addRow(r)
        tip = QLabel("Consejo: 10-20 s de una sola persona hablando claro, sin música ni ruido de fondo.")
        tip.setObjectName("status")
        f.addRow(tip)
        lay.addWidget(g)

        g = QGroupBox("Ajustes")
        f = QFormLayout(g)
        s = self.cfg["clone"]
        self.mode = QComboBox()
        self.mode.addItem("Rápido (~2 s): Kokoro + conversión de voz", "rapido")
        self.mode.addItem("Completo (4-9 s): imita también la forma de hablar", "completo")
        self.mode.setCurrentIndex(0 if s["mode"] == "rapido" else 1)
        self.mode.currentIndexChanged.connect(
            lambda: (self._setting("mode", self.mode.currentData()), self.win._warmup_clone()))
        f.addRow("Modo:", self.mode)
        self.steps = QComboBox()
        for n, label in ((4, "4 pasos — más rápido"), (6, "6 pasos — equilibrado"), (10, "10 pasos — máximo")):
            self.steps.addItem(label, n)
        self.steps.setCurrentIndex(self.steps.findData(s["steps"]))
        self.steps.currentIndexChanged.connect(lambda: self._setting("steps", self.steps.currentData()))
        f.addRow("Calidad (modo rápido):", self.steps)
        base = speech.kokoro_base_voices()
        for key, label in (("base_en", "Voz base inglés:"), ("base_es", "Voz base español:")):
            box = QComboBox()
            for v in base:
                box.addItem(speech.kokoro_label(v), v)
            box.setCurrentIndex(max(0, box.findData(s[key])))
            box.currentIndexChanged.connect(lambda _i, k=key, b=box: self._setting(k, b.currentData()))
            f.addRow(label, box)
        hint = QLabel("La voz base pone la entonación en el modo rápido: elige una del mismo género que la voz "
                      "clonada para que suene más natural.")
        hint.setWordWrap(True)
        hint.setObjectName("status")
        f.addRow(hint)
        lay.addWidget(g)
        close = QPushButton("Cerrar")
        close.clicked.connect(self.accept)
        lay.addWidget(close, 0, Qt.AlignRight)
        self._refresh()

    def _setting(self, key, value):
        self.cfg["clone"][key] = value
        self.cfg.save()

    def _refresh(self):
        self.list.clear()
        self.list.addItems(clone.voice_names())

    def _selected(self):
        item = self.list.currentItem()
        return item.text() if item else None

    def _span(self):
        try:
            start = float(self.start.text().replace(",", ".") or 0)
            end = float(self.end.text().replace(",", ".")) if self.end.text().strip() else None
        except ValueError:
            raise ValueError("Los segundos deben ser números, p. ej. 12.5")
        return start, end

    def _add(self, source):
        name = self.name.text().strip() or Path(str(source)).stem
        try:
            start, end = self._span()
            name = clone.add_voice(name, source, start, end)
        except Exception as e:
            QMessageBox.warning(self, "Voz clonada", str(e))
            return
        self._refresh()
        self.list.setCurrentRow(clone.voice_names().index(name))
        self.win.bridge.status.emit(f"Voz «{name}» añadida: elígela en Motor «Voces clonadas»")

    def _from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Audio con la voz", "", "Audio y vídeo (" + " ".join(f"*{e}" for e in sorted(library.ALL_EXTS)) + ")")
        if path:
            self._add(path)

    def _record(self, on):
        if on:
            try:
                self.win.recorder.start(audio.find_device(self.cfg["mic_device"], "input"))
                self.rec_btn.setText("⏹ Parar grabación (habla 10-20 s)")
            except Exception as e:
                self.rec_btn.setChecked(False)
                QMessageBox.warning(self, "Micro", str(e))
            return
        self.rec_btn.setText("🎙 Grabar con mi micro")
        data, sr = self.win.recorder.stop(raw=True)
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            sf.write(tmp, data, sr, subtype="FLOAT")
            if not self.name.text().strip():
                self.name.setText("Mi voz")
            self.start.setText("0")
            self.end.setText("")
            self._add(tmp)
        finally:
            os.remove(tmp)

    def _play_ref(self):
        name = self._selected()
        if name:
            self.win.engine.preview(*audio.load_audio(clone.VOICES_DIR / f"{name}.wav"))

    def _test(self):
        name = self._selected()
        if not name:
            return
        es = speech.MODES[self.cfg["voice_mode"]][1] == "es"
        voice = f"{'es-es' if es else 'en-us'} · 🧬 {name}"
        text = "Hola, esta es mi voz clonada. ¿Qué tal sueno?" if es else "Hello, this is my cloned voice. How do I sound?"
        self.win.bridge.status.emit("Generando prueba con la voz clonada (la primera vez tarda ~1 min)…")

        def job():
            self.win.engine.preview(*clone.clone_synth(text, voice, 0, 0))
            self.win.bridge.status.emit("Prueba lista ✔")
        self.win._run(job)

    def _use(self):
        name = self._selected()
        if name:
            self.win.use_my_voice(("clon", "🧬", name))
            self.win._warmup_clone()
            self.accept()

    def _delete(self):
        name = self._selected()
        if name and QMessageBox.question(self, "Borrar", f"¿Borrar la voz «{name}»?") == QMessageBox.Yes:
            clone.remove_voice(name)
            self._refresh()


def run():
    import sys
    from PySide6.QtGui import QIcon
    # Que la barra de tareas muestre el icono propio y no el de Python
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GhostVoice.App")
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent.parent / "ghostvoice.ico")))
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
