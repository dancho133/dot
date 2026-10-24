"""
dot — сверхбыстрая диктовка голосом на Windows без нагрузки на ПК.

Жмёшь Ctrl+Alt+D (или кликаешь точку) — говоришь — жмёшь ещё раз.
Текст сразу вставляется под курсор и копируется в системный буфер (Ctrl+V).
Распознавание: Groq Whisper Cloud (whisper-large-v3-turbo, ~0.2-0.4 с).
Нагрузка: 0 МБ VRAM, ~25 МБ ОЗУ в фоне.

Ctrl+Alt+F — самообучение: копируешь исправленный текст в буфер, жмёшь хоткей,
dot сравнивает со своей последней расшифровкой и запоминает разницу в vocab.txt и corrections.json.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import difflib
import json
import math
import os
import re
import sys
import threading
import time
import wave
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

HERE = Path(__file__).parent
VOCAB_FILE = HERE / "vocab.txt"
CORRECTIONS_FILE = HERE / "corrections.json"
STATE_FILE = HERE / "state.json"
HISTORY_FILE = HERE / "history.jsonl"
LAST_AUDIO_FILE = HERE / "last.wav"

DEFAULT_GROQ_KEY = ""
DEFAULT_MODEL = "whisper-large-v3-turbo"
LANGUAGE = "ru"
SAMPLE_RATE = 16000

STYLE_PROMPT = "Запишу мысль. Во-первых, про проект: план, сроки, деньги. Дальше — что делать?"

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
_u32.RegisterHotKey.restype = wt.BOOL
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = wt.HANDLE

MUTEX_NAME = "dot-dictation-single-instance"
ERROR_ALREADY_EXISTS = 183
MB_ICONWARNING = 0x30


def alarm(text: str) -> None:
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    _u32.MessageBoxW(None, text, "dot", MB_ICONWARNING)


def already_running(name: str = MUTEX_NAME) -> bool:
    _k32.CreateMutexW(None, True, name)
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY = 0x0312
VK_CONTROL, VK_MENU, VK_SHIFT, VK_V = 0x11, 0x12, 0x10, 0x56
KEYEVENTF_KEYUP = 0x2

HOTKEY_TALK = (MOD_CONTROL | MOD_ALT, 0x44, "Ctrl+Alt+D")
HOTKEY_LEARN = (MOD_CONTROL | MOD_ALT, 0x46, "Ctrl+Alt+F")
HOTKEY_TALK_ID, HOTKEY_LEARN_ID = 1, 2

VOCAB_TERMS_KEPT = 60
METER_FLOOR_DB = -50.0
METER_CEIL_DB = -5.0

INK = QColor(0x17, 0x13, 0x0F)
LIP = QColor(0x4A, 0x41, 0x36)
BONE = QColor(0xE4, 0xDC, 0xCD)
CLAY = QColor(0x9A, 0x5F, 0x42)

WIN = 72
CTR = WIN / 2
R_DISC_SHUT = 15.0
R_DISC_OPEN = 24.0
R_TICK_IN = 14.5
R_TICK_OUT = 20.5
TICKS = 20

IDLE, LOADING, REC, WORK, ERR = "idle", "loading", "rec", "work", "err"


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        val = json.loads(STATE_FILE.read_text("utf-8"))
        return val if isinstance(val, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(**updates: object) -> None:
    st = read_state()
    st.update(updates)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), "utf-8")


def groq_api_key() -> str:
    return (
        os.environ.get("GROQ_API_KEY", "").strip()
        or str(read_state().get("groq_api_key", "")).strip()
        or DEFAULT_GROQ_KEY
    )


def groq_model() -> str:
    return (
        os.environ.get("GROQ_MODEL", "").strip()
        or str(read_state().get("groq_model", "")).strip()
        or DEFAULT_MODEL
    )


def read_vocab() -> str | None:
    if not VOCAB_FILE.exists():
        return None
    terms = [t.strip() for t in VOCAB_FILE.read_text("utf-8").splitlines() if t.strip()]
    return ", ".join(terms[-VOCAB_TERMS_KEPT:]) or None


def add_to_vocab(terms: list[str]) -> None:
    have = []
    if VOCAB_FILE.exists():
        have = [t.strip() for t in VOCAB_FILE.read_text("utf-8").splitlines() if t.strip()]
    lower = {t.lower() for t in have}
    fresh = [t for t in terms if t.lower() not in lower]
    if fresh:
        VOCAB_FILE.write_text("\n".join(have + fresh) + "\n", "utf-8")


def load_corrections() -> dict[str, str]:
    if not CORRECTIONS_FILE.exists():
        return {}
    try:
        return json.loads(CORRECTIONS_FILE.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}


def apply_corrections(text: str) -> str:
    for wrong, right in sorted(load_corrections().items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text, flags=re.IGNORECASE)
    return text


def learn(raw: str, fixed: str) -> int:
    a, b = raw.split(), fixed.split()
    if not a or not b:
        return 0
    matcher = difflib.SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in b])
    pairs: dict[str, str] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace" or i2 - i1 > 3 or j2 - j1 > 3:
            continue
        wrong = " ".join(a[i1:i2]).strip(".,!?;:-—«»\"'")
        right = " ".join(b[j1:j2]).strip(".,!?;:-—«»\"'")
        if wrong and right and wrong.lower() != right.lower():
            pairs[wrong] = right
    if not pairs:
        return 0
    table = load_corrections()
    table.update(pairs)
    CORRECTIONS_FILE.write_text(json.dumps(table, ensure_ascii=False, indent=2), "utf-8")
    add_to_vocab(list(pairs.values()))
    return len(pairs)


def wav_bytes(audio: np.ndarray) -> bytes:
    stream = BytesIO()
    with wave.open(stream, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return stream.getvalue()


def normalize(audio: np.ndarray) -> np.ndarray:
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak < 1e-4:
        return audio
    return (audio * (0.7 / peak)).astype(np.float32)


def save_audio(audio: np.ndarray) -> None:
    LAST_AUDIO_FILE.write_bytes(wav_bytes(audio))


def groq_transcribe(audio: np.ndarray) -> str:
    key = groq_api_key()
    if not key:
        raise RuntimeError("Groq API key не задан")
    model = groq_model()
    audio_data = wav_bytes(normalize(audio))

    prompt = STYLE_PROMPT
    vocab = read_vocab()
    if vocab:
        prompt = f"{STYLE_PROMPT} Словарь: {vocab}"

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": "dot-voice/2.0"
    }
    files = {
        "file": ("dictation.wav", audio_data, "audio/wav")
    }
    data = {
        "model": model,
        "language": LANGUAGE,
        "response_format": "json",
        "prompt": prompt
    }

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, headers=headers, files=files, data=data)
        if resp.status_code != 200:
            err_msg = resp.text[:150]
            raise RuntimeError(f"Groq HTTP {resp.status_code}: {err_msg}")
        payload = resp.json()
        text = payload.get("text", "")
        if not isinstance(text, str):
            raise RuntimeError("Groq вернул неполный ответ")
        return text.strip()


def groq_healthcheck() -> str:
    key = groq_api_key()
    if not key:
        raise RuntimeError("Groq API key не задан")
    url = "https://api.groq.com/openai/v1/models"
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": "dot-voice/2.0"
    }
    with httpx.Client(timeout=8.0) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Groq HTTP {resp.status_code}")
    return f"Groq ({groq_model()})"


class Recorder:
    def __init__(self) -> None:
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self.level = 0.0

    def _callback(self, indata, frames, time_info, status) -> None:
        with self._lock:
            self._frames.append(indata.copy())
        rms = float(np.sqrt(np.mean(np.square(indata))))
        db = 20.0 * math.log10(max(rms, 1e-7))
        span = METER_CEIL_DB - METER_FLOOR_DB
        self.level = min(1.0, max(0.0, (db - METER_FLOOR_DB) / span))

    def start(self) -> None:
        with self._lock:
            self._frames = []
        self.level = 0.0
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=512, callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.level = 0.0
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._frames, axis=0).ravel()


class Bridge(QObject):
    talk = Signal()
    teach = Signal()
    ready = Signal(str)
    done = Signal(str)
    failed = Signal()


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", ctypes.c_uint),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", wt.POINT),
    ]


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, handlers: dict[int, object]) -> None:
        super().__init__()
        self.handlers = handlers

    def nativeEventFilter(self, event_type, message):
        msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
        if msg.message == WM_HOTKEY:
            handler = self.handlers.get(int(msg.wParam))
            if handler is not None:
                handler()
        return False, 0


class Dot(QWidget):
    def __init__(self, live: bool = True) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(WIN, WIN)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

        self.state = LOADING
        self.hover = False
        self.level = 0.0
        self.peak = 0.0
        self.phase = 0.0
        self.disc_r = R_DISC_SHUT
        self.last_raw = ""
        self.backend_status = "проверяется..."
        self._press: QPointF | None = None
        self._moved = False

        self._apply_saved_position()

        if not live:
            self.state = IDLE
            return

        self.recorder = Recorder()
        self.bridge = Bridge()
        self.bridge.talk.connect(self.toggle)
        self.bridge.teach.connect(self.teach)
        self.bridge.ready.connect(self._on_ready)
        self.bridge.done.connect(self._on_done)
        self.bridge.failed.connect(self._on_failed)

        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self._tick)
        self.ticker.start(16)

        threading.Thread(target=self._load_model, daemon=True).start()

        self._filter = HotkeyFilter(
            {HOTKEY_TALK_ID: self.bridge.talk.emit, HOTKEY_LEARN_ID: self.bridge.teach.emit}
        )
        QApplication.instance().installNativeEventFilter(self._filter)

        hwnd = int(self.winId())
        for hid, (mod, vk, name) in (
            (HOTKEY_TALK_ID, HOTKEY_TALK),
            (HOTKEY_LEARN_ID, HOTKEY_LEARN),
        ):
            if not _u32.RegisterHotKey(hwnd, hid, mod | MOD_NOREPEAT, vk):
                alarm(f"Хоткей {name} занят другой программой.")

    def _apply_saved_position(self) -> None:
        geom = QGuiApplication.primaryScreen().availableGeometry()
        st = read_state()
        x = st.get("x", geom.right() - WIN - 24)
        y = st.get("y", geom.bottom() - WIN - 24)
        x = max(geom.left(), min(int(x), geom.right() - WIN))
        y = max(geom.top(), min(int(y), geom.bottom() - WIN))
        self.move(x, y)

    def save_position(self) -> None:
        write_state(x=self.x(), y=self.y())

    def _load_model(self) -> None:
        try:
            status = groq_healthcheck()
            self.bridge.ready.emit(status)
        except Exception as exc:
            self.bridge.ready.emit(f"fail:{type(exc).__name__}")

    def _on_ready(self, info: str) -> None:
        if info.startswith("fail:"):
            alarm(f"Groq Cloud недоступен: {info[5:]}. Проверьте подключение к интернету.")
            self._on_failed()
            return
        self.state = IDLE
        self.backend_status = info
        print(f"dot готов [{info}]. {HOTKEY_TALK[2]} — говорить, {HOTKEY_LEARN[2]} — научить.")

    def toggle(self) -> None:
        if self.state == IDLE:
            try:
                self.recorder.start()
            except Exception as exc:
                alarm(f"Микрофон не открылся: {exc}")
                self._on_failed()
                return
            self.state = REC
        elif self.state == REC:
            audio = self.recorder.stop()
            self.state = WORK
            threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    def _transcribe(self, audio: np.ndarray) -> None:
        if audio.size < SAMPLE_RATE // 4:
            self.bridge.done.emit("")
            return
        try:
            save_audio(audio)
            raw = groq_transcribe(audio)
        except Exception as exc:
            alarm(f"Ошибка распознавания: {exc}")
            self.bridge.failed.emit()
            return
        self.bridge.done.emit(raw)

    def _on_done(self, raw: str) -> None:
        self.state = IDLE
        if not raw:
            return
        self.last_raw = raw
        text = apply_corrections(raw)
        self.remember(text)
        self.paste(text)

    def remember(self, text: str) -> None:
        entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text}
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def cancel(self) -> None:
        if self.state == REC:
            self.recorder.stop()
            self.state = IDLE

    def _on_failed(self) -> None:
        self.state = ERR
        QTimer.singleShot(2000, self._recover)

    def _recover(self) -> None:
        if self.state == ERR:
            self.state = IDLE

    def paste(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        for _ in range(60):
            held = any(
                _u32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_CONTROL, VK_MENU, VK_SHIFT)
            )
            if not held:
                break
            time.sleep(0.02)
        time.sleep(0.05)
        for vk, up in ((VK_CONTROL, 0), (VK_V, 0), (VK_V, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)):
            _u32.keybd_event(vk, 0, up, 0)

    def teach(self) -> None:
        fixed = QApplication.clipboard().text().strip()
        if not self.last_raw or not fixed:
            return
        n = learn(self.last_raw, fixed)
        print(f"Запомнено правок: {n}" if n else "Отличий не найдено")

    def show_menu(self, position) -> None:
        menu = QMenu(self)
        status_action = menu.addAction(f"Движок: {self.backend_status}")
        status_action.setEnabled(False)
        menu.addSeparator()
        hotkey_info = menu.addAction("Ctrl+Alt+D: Запись | Ctrl+Alt+F: Обучить")
        hotkey_info.setEnabled(False)
        menu.addSeparator()
        quit_action = menu.addAction("Выход")
        chosen = menu.exec(position)
        if chosen is quit_action:
            QApplication.quit()

    def enterEvent(self, event) -> None:
        self.hover = True

    def leaveEvent(self, event) -> None:
        self.hover = False

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press = event.globalPosition() - QPointF(self.x(), self.y())
            self._moved = False
        elif event.button() == Qt.RightButton:
            if self.state == REC:
                self.cancel()
            elif self.state == IDLE:
                self.show_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:
        if self._press is None:
            return
        target = event.globalPosition() - self._press
        if not self._moved and (target - QPointF(self.x(), self.y())).manhattanLength() > 4:
            self._moved = True
        if self._moved:
            self.move(target.toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if self._press is None:
            return
        self._press = None
        if self._moved:
            self.save_position()
        else:
            self.toggle()

    def target_disc(self) -> float:
        return R_DISC_OPEN if self.state in (REC, WORK, LOADING) else R_DISC_SHUT

    def _tick(self) -> None:
        target = self.recorder.level if self.state == REC else 0.0
        self.level += (target - self.level) * (0.5 if target > self.level else 0.09)
        self.peak = max(target, self.peak - 0.012)
        self.disc_r += (self.target_disc() - self.disc_r) * 0.18
        if self.state in (WORK, LOADING):
            self.phase += 0.075 if self.state == WORK else 0.028
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QPointF(CTR, CTR)
        r = self.disc_r

        disc = QColor(0x24, 0x18, 0x12) if self.state == REC else QColor(INK)
        disc.setAlpha(242)
        p.setPen(Qt.NoPen)
        p.setBrush(disc)
        p.drawEllipse(c, r, r)

        lip = QLinearGradient(0, CTR - r, 0, CTR + r)
        top = QColor(LIP)
        top.setAlpha(255 if self.hover else 190)
        lip.setColorAt(0.0, top)
        lip.setColorAt(0.55, QColor(0x2A, 0x24, 0x1D, 170))
        lip.setColorAt(1.0, QColor(0x0B, 0x09, 0x07, 210))
        p.setPen(QPen(lip, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, r - 0.6, r - 0.6)

        if self.state == ERR:
            core = QColor(CLAY)
            radius = 4.5
        else:
            core = QColor(BONE)
            core.setAlpha(255 if self.hover or self.state != IDLE else 205)
            radius = 4.5 + self.level * 2.2
        p.setPen(Qt.NoPen)
        p.setBrush(core)
        p.drawEllipse(c, radius, radius)

        if self.state == REC:
            self._draw_meter(p)
        elif self.state in (WORK, LOADING):
            self._draw_sweep(p)

    def _face(self) -> tuple[float, float, float]:
        scale = self.disc_r / R_DISC_OPEN
        fade = (self.disc_r - R_DISC_SHUT) / (R_DISC_OPEN - R_DISC_SHUT)
        return R_TICK_IN * scale, R_TICK_OUT * scale, min(1.0, max(0.0, fade))

    def _tick_pen(self, colour: QColor, alpha: float) -> QPen:
        colour.setAlpha(int(min(255.0, max(0.0, alpha))))
        pen = QPen(colour, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        return pen

    def _draw_meter(self, p: QPainter) -> None:
        r_in, r_out, fade = self._face()
        lit = self.level * TICKS
        peak_tick = int(self.peak * TICKS)
        for i in range(TICKS):
            angle = math.radians(-90 + i * (360 / TICKS))
            on = min(1.0, max(0.0, lit - i))
            if i == peak_tick and self.peak > 0.02:
                alpha = 255.0
            elif on > 0:
                alpha = 90 + 150 * on
            else:
                alpha = 38.0
            p.setPen(self._tick_pen(QColor(BONE), alpha * fade))
            p.drawLine(self._on_ring(angle, r_in), self._on_ring(angle, r_out))

    def _draw_sweep(self, p: QPainter) -> None:
        r_in, r_out, fade = self._face()
        dim = 1.0 if self.state == WORK else 0.5
        lead = (self.phase * TICKS / math.pi) % TICKS
        for i in range(TICKS):
            angle = math.radians(-90 + i * (360 / TICKS))
            behind = (lead - i) % TICKS
            alpha = 38 + 200 * max(0.0, 1.0 - behind / 5.0)
            p.setPen(self._tick_pen(QColor(BONE), alpha * fade * dim))
            p.drawLine(self._on_ring(angle, r_in), self._on_ring(angle, r_out))

    @staticmethod
    def _on_ring(angle: float, radius: float) -> QPointF:
        return QPointF(CTR + math.cos(angle) * radius, CTR + math.sin(angle) * radius)


def main() -> None:
    if already_running():
        alarm("dot уже запущен. Нажмите Ctrl+Alt+D для диктовки.")
        sys.exit(1)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    dot = Dot()
    dot.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
