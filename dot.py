"""
dot — локальная диктовка.

Жмёшь Ctrl+Alt+D (или кликаешь точку) — говоришь — жмёшь ещё раз.
Текст сам вставляется туда, где стоит курсор. В интернет ничего не уходит.

Ctrl+Alt+F — научить: копируешь исправленный текст в буфер, жмёшь,
dot сравнивает со своей последней расшифровкой и запоминает разницу.
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
from pathlib import Path

# CUDA-библиотеки приезжают пакетами nvidia-*, но Windows сама их не находит.
# Обязательно до импорта faster_whisper, иначе он подхватит пустоту.
# Только PATH: ctranslate2 грузит cublas по имени, а такой вызов ищет именно
# в PATH и НЕ смотрит в os.add_dll_directory.
try:
    import nvidia  # namespace-пакет: у него нет __file__, только __path__

    _bins = [str(b) for root in nvidia.__path__ for b in Path(root).glob("*/bin")]
    if _bins:
        os.environ["PATH"] = os.pathsep.join(_bins) + os.pathsep + os.environ.get("PATH", "")
except ImportError:
    pass

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

HERE = Path(__file__).parent
VOCAB_FILE = HERE / "vocab.txt"
CORRECTIONS_FILE = HERE / "corrections.json"
STATE_FILE = HERE / "state.json"

MODEL = "large-v3-turbo"
LANGUAGE = "ru"
SAMPLE_RATE = 16000

# --- клавиатура через Windows напрямую ----------------------------------------
# Библиотека keyboard разбирает клавиши по буквам ТЕКУЩЕЙ раскладки: при русской
# она на клавише "A" видит "ф" и hotkey "ctrl+alt+d" не узнаёт никогда, молча.
# RegisterHotKey работает с кодами клавиш, раскладке безразличен и честно
# сообщает, если комбинация уже занята.
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
_u32.RegisterHotKey.restype = wt.BOOL

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY = 0x0312
VK_CONTROL, VK_MENU, VK_SHIFT, VK_V = 0x11, 0x12, 0x10, 0x56
KEYEVENTF_KEYUP = 0x2
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# (модификаторы, код клавиши, как назвать человеку)
HOTKEY_TALK = (MOD_CONTROL | MOD_ALT, 0x44, "Ctrl+Alt+D")
HOTKEY_LEARN = (MOD_CONTROL | MOD_ALT, 0x46, "Ctrl+Alt+F")
HOTKEY_TALK_ID, HOTKEY_LEARN_ID = 1, 2

VOCAB_TERMS_KEPT = 60  # initial_prompt у Whisper вмещает ~224 токена, дальше он его режет

# Шкала индикатора, в децибелах. Это ручка настройки под конкретный микрофон:
# если риски еле шевелятся, когда ты говоришь, — подними METER_FLOOR_DB
# (например до -40). На распознавание не влияет вообще, только на картинку:
# Whisper разбирает речь даже на -70 дБ, проверено.
METER_FLOOR_DB = -50.0
METER_CEIL_DB = -5.0

# Тёплый уголь и кость. Одна глиняная нота на состояние — больше цветов нет.
INK = QColor(0x17, 0x13, 0x0F)
LIP = QColor(0x4A, 0x41, 0x36)
BONE = QColor(0xE4, 0xDC, 0xCD)
CLAY = QColor(0x9A, 0x5F, 0x42)

WIN = 72
CTR = WIN / 2
R_DISC_SHUT = 15.0  # отдыхает
R_DISC_OPEN = 24.0  # слушает или думает
# Риски живут ВНУТРИ диска. Снаружи они тонули в белом чате: кость по белому не
# видно, а фон под точкой мы не выбираем. Диск — единственная поверхность,
# чей цвет мы знаем, поэтому индикатор рисуем только на нём.
R_TICK_IN = 14.5
R_TICK_OUT = 20.5
TICKS = 20

IDLE, LOADING, REC, WORK, ERR = "idle", "loading", "rec", "work", "err"


# --- словарь и обучение ------------------------------------------------------


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
    # Длинные фразы заменяем раньше коротких, иначе короткая съест кусок длинной.
    for wrong, right in sorted(load_corrections().items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text, flags=re.IGNORECASE)
    return text


def learn(raw: str, fixed: str) -> int:
    """Сравнить сырую расшифровку с исправленной, запомнить замены. Вернуть их число."""
    a, b = raw.split(), fixed.split()
    if not a or not b:
        return 0
    matcher = difflib.SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in b])
    pairs: dict[str, str] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        # ponytail: только короткие замены. Длинный кусок — это он переписал мысль,
        # а не поправил распознавание, и в словарь ему нельзя.
        if tag != "replace" or i2 - i1 > 3 or j2 - j1 > 3:
            continue
        wrong = " ".join(a[i1:i2]).strip(".,!?;:—-«»\"'")
        right = " ".join(b[j1:j2]).strip(".,!?;:—-«»\"'")
        if wrong and right and wrong.lower() != right.lower():
            pairs[wrong] = right
    if not pairs:
        return 0
    table = load_corrections()
    table.update(pairs)
    CORRECTIONS_FILE.write_text(json.dumps(table, ensure_ascii=False, indent=2), "utf-8")
    add_to_vocab(list(pairs.values()))
    return len(pairs)


# --- звук --------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self.level = 0.0  # 0..1, уже в дБ-шкале — так меряют настоящие индикаторы

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


class Transcriber:
    def __init__(self) -> None:
        self.model = None
        self.device = "?"
        self.error: Exception | None = None

    def load(self) -> None:
        from faster_whisper import WhisperModel

        last: Exception | None = None
        for device, compute in (("cuda", "float16"), ("cpu", "int8")):
            try:
                self.model = WhisperModel(MODEL, device=device, compute_type=compute)
                # Модель охотно создаётся на CUDA и падает только на первом же
                # распознавании (нет cublas). Поэтому проверяем настоящим звуком,
                # а не фактом загрузки — иначе откат на процессор не сработает.
                probe = np.random.default_rng(0).normal(0, 0.05, SAMPLE_RATE).astype(np.float32)
                list(self.model.transcribe(probe, language=LANGUAGE, vad_filter=False)[0])
                self.device = device
                return
            except Exception as exc:  # нет CUDA/cuDNN — не беда, поедем на процессоре
                last = exc
                self.model = None
                print(f"{device} не вышло ({exc}), пробую дальше", file=sys.stderr)
        self.error = last

    def run(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language=LANGUAGE,
            beam_size=5,
            initial_prompt=read_vocab(),
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()


# --- мост из чужих потоков в поток Qt ----------------------------------------


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
    """Ловит WM_HOTKEY до того, как Qt его выбросит."""

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


# --- точка -------------------------------------------------------------------


class Dot(QWidget):
    def __init__(self, live: bool = True) -> None:
        """live=False — только внешний вид, без микрофона, модели и клавиш (для preview.py)."""
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
        self._press: QPointF | None = None
        self._moved = False

        self.recorder = Recorder()
        self.transcriber = Transcriber()

        self.bridge = Bridge()
        self.bridge.talk.connect(self.toggle)
        self.bridge.teach.connect(self.teach)
        self.bridge.ready.connect(self._on_ready)
        self.bridge.done.connect(self._on_done)
        self.bridge.failed.connect(self._on_failed)

        if not live:
            return

        self.restore_position()

        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self._tick)
        self.ticker.start(16)

        threading.Thread(target=self._load_model, daemon=True).start()

        self._filter = HotkeyFilter(
            {HOTKEY_TALK_ID: self.bridge.talk.emit, HOTKEY_LEARN_ID: self.bridge.teach.emit}
        )
        QApplication.instance().installNativeEventFilter(self._filter)
        self.register_hotkeys()

    def register_hotkeys(self) -> None:
        for key_id, (mods, vk, label) in (
            (HOTKEY_TALK_ID, HOTKEY_TALK),
            (HOTKEY_LEARN_ID, HOTKEY_LEARN),
        ):
            ctypes.set_last_error(0)
            # Вешаем на поток (hwnd=None), а не на окно. Сообщение, адресованное
            # окну Qt, проходит через нативный фильтр ДВАЖДЫ — из очереди и из
            # оконной процедуры, — и каждое нажатие срабатывало бы два раза:
            # запись включалась и тут же выключалась.
            if _u32.RegisterHotKey(None, key_id, mods | MOD_NOREPEAT, vk):
                continue
            err = ctypes.get_last_error()
            # Молча проглотить нельзя: клавиша просто не работала бы, и человек
            # думал бы, что сломана программа, а не что комбинацию занял сосед.
            why = (
                "её уже занял кто-то другой"
                if err == ERROR_HOTKEY_ALREADY_REGISTERED
                else f"ошибка Windows {err}"
            )
            print(f"клавиша {label} не работает: {why}", file=sys.stderr)

    # позиция

    def restore_position(self) -> None:
        area = QGuiApplication.primaryScreen().availableGeometry()
        x, y = area.right() - WIN - 24, area.bottom() - WIN - 24
        if STATE_FILE.exists():
            try:
                saved = json.loads(STATE_FILE.read_text("utf-8"))
                x, y = int(saved["x"]), int(saved["y"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                pass
        # Точка могла остаться от монитора, который отключили.
        x = min(max(x, area.left()), area.right() - WIN)
        y = min(max(y, area.top()), area.bottom() - WIN)
        self.move(x, y)

    def save_position(self) -> None:
        STATE_FILE.write_text(json.dumps({"x": self.x(), "y": self.y()}), "utf-8")

    # состояния

    def _load_model(self) -> None:
        self.transcriber.load()
        self.bridge.ready.emit("" if self.transcriber.model else "fail")

    def _on_ready(self, problem: str) -> None:
        if problem:
            print(f"модель не загрузилась: {self.transcriber.error}", file=sys.stderr)
            self.state = ERR
            return
        self.state = IDLE
        print(
            f"dot готов, модель {MODEL} на {self.transcriber.device}. "
            f"{HOTKEY_TALK[2]} — говорить, {HOTKEY_LEARN[2]} — научить."
        )

    def toggle(self) -> None:
        if self.state == IDLE:
            self.recorder.start()
            self.state = REC
        elif self.state == REC:
            audio = self.recorder.stop()
            self.state = WORK
            threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    def _transcribe(self, audio: np.ndarray) -> None:
        # Меньше четверти секунды — это промах по клавише, а не речь.
        if audio.size < SAMPLE_RATE // 4:
            self.bridge.done.emit("")
            return
        try:
            raw = self.transcriber.run(audio)
        except Exception as exc:
            print(f"расшифровка сорвалась: {exc}", file=sys.stderr)
            self.bridge.failed.emit()
            return
        self.bridge.done.emit(raw)

    def _on_done(self, raw: str) -> None:
        self.state = IDLE
        if not raw:
            return
        self.last_raw = raw
        self.paste(apply_corrections(raw))

    def _on_failed(self) -> None:
        self.state = ERR
        QTimer.singleShot(2000, self._recover)

    def _recover(self) -> None:
        if self.state == ERR and self.transcriber.model is not None:
            self.state = IDLE

    def paste(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        # Ctrl/Alt/Shift могут быть ещё зажаты от горячей клавиши — тогда прилетит
        # не Ctrl+V, а Ctrl+Alt+V, и никакой вставки не будет.
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
        print(f"запомнил замен: {n}" if n else "разницы не нашёл")

    # мышь

    def enterEvent(self, event) -> None:
        self.hover = True

    def leaveEvent(self, event) -> None:
        self.hover = False

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press = event.globalPosition() - QPointF(self.x(), self.y())
            self._moved = False

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

    # рисование

    def target_disc(self) -> float:
        """Открыт — значит слушает или думает. Закрыт — отдыхает."""
        return R_DISC_OPEN if self.state in (REC, WORK, LOADING) else R_DISC_SHUT

    def _tick(self) -> None:
        target = self.recorder.level if self.state == REC else 0.0
        # Вверх индикатор летит быстро, вниз оседает медленно — как настоящий VU.
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

        # Диск. Глубина — от тона и от кромки, ловящей свет сверху. Без теней.
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

        # Сама точка.
        if self.state == ERR:
            core = QColor(CLAY)
            radius = 4.5
        else:
            core = QColor(BONE)
            # Наведение меняет только тон и кромку. Точка под курсором не растёт и
            # не прыгает: подскок на hover — дешёвый рефлекс, а не взаимодействие.
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
        """Риски масштабируются вместе с диском, иначе на полпути вылезут за кромку."""
        scale = self.disc_r / R_DISC_OPEN
        fade = (self.disc_r - R_DISC_SHUT) / (R_DISC_OPEN - R_DISC_SHUT)
        return R_TICK_IN * scale, R_TICK_OUT * scale, min(1.0, max(0.0, fade))

    def _tick_pen(self, colour: QColor, alpha: float) -> QPen:
        colour.setAlpha(int(min(255.0, max(0.0, alpha))))
        pen = QPen(colour, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        return pen

    def _draw_meter(self, p: QPainter) -> None:
        """Кольцо рисок — это настоящий уровень с микрофона, а не украшение."""
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
        """Та же геометрия рисок, но одна бежит по кругу — пока думает."""
        r_in, r_out, fade = self._face()
        # Пока грузится — тот же свет, просто тише. Глиняным он тонул в диске.
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
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    dot = Dot()
    dot.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
