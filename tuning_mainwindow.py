#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phaco Tuning  —  v4                                                    ║
# ║  • Dual-channel sweep  : 0xA7 (CH2 Phaco current) + 0xE7 (CH3 Voltage) ║
# ║  • Raw register data   : every sample of every channel saved to TXT     ║
# ║  • UI                  : white-canvas plot, visible labels, info bar    ║
# ║  • FIX: CH2=orange/yellow, CH3=blue  (matches physical display)         ║
# ║  • FIX: X-axis range locked — no zoom-in after sweep completes          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

import os, sys, time, signal, threading, ctypes, ctypes.util
from datetime import datetime

from PyQt5.QtCore    import Qt, QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QPushButton, QLabel, QVBoxLayout, QHBoxLayout,
    QLineEdit, QDialog, QGridLayout, QFrame
)
from PyQt5.QtGui import QColor, QPainter, QPixmap, QPen, QFont

# ──────────────────────────────────────────────────────────────────────────────
# EXIT HANDLER
# ──────────────────────────────────────────────────────────────────────────────
def handle_exit(sig, frame):
    print("\n[INFO] Exit"); sys.exit(0)

signal.signal(signal.SIGINT,  handle_exit)
signal.signal(signal.SIGTERM, handle_exit)

os.system("fbset -fb /dev/fb0 -yoffset 0 2>/dev/null")
os.environ["QT_QPA_PLATFORM"]         = "linuxfb:fb=/dev/fb0:noblit:nographicsmodeswitch"
os.environ["QT_QPA_FB_DISABLE_INPUT"] = "0"
try:
    open("/sys/class/graphics/fb0/blank", "w").write("0")
except Exception:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────
ADS7841_DEVICE = "/dev/spidev2.0"
LTC2604_DEVICE = "/dev/spidev2.1"

SPI_MODE  = 0
SPI_BITS  = 8
SPI_SPEED = 1_000_000

SPI_IOC_WR_MODE          = 0x40016b01
SPI_IOC_WR_BITS_PER_WORD = 0x40016b03
SPI_IOC_WR_MAX_SPEED_HZ  = 0x40046b04

ADC_FULLSCALE = 4095

# ── ADS7841 channel command bytes ─────────────────────────────────────────────
CH_0xA7 = 0xA7   # CH2  — Phaco current  (primary sweep channel)
CH_0xE7 = 0xE7   # CH3  — Voltage sensor (secondary sweep channel)
CH_0x97 = 0x97   # CH0  — FS feedback    (stored but not plotted by default)
CH_0xD7 = 0xD7   # CH1  — Sensor         (stored but not plotted by default)

# ── FPGA register map ─────────────────────────────────────────────────────────
XPAR_BASE       = 0x43C20000
MAP_SIZE        = 4096
MAP_MASK        = MAP_SIZE - 1

REG_PHACO_ONOFF = 0x00
REG_FS_COUNT    = 0x02
REG_PULSE_COUNT = 0x04
REG_PDM_MODE    = 0x06
REG_FREQ_COUNT  = 0x0C
REG_TUNE_REQ    = 0x0E

TUNE_REQUEST_MASK = 0x8000
PDM_CONTINUOUS    = 0x01
SAMPLES_PER_STEP  = 128   # averages per frequency step (matches C++)

Y_TICKS = [0, 1000, 2000, 3000, 4000, 4095]

# ──────────────────────────────────────────────────────────────────────────────
# SPI STRUCT
# ──────────────────────────────────────────────────────────────────────────────
class SpiIocTransfer(ctypes.Structure):
    _fields_ = [
        ("tx_buf",           ctypes.c_uint64),
        ("rx_buf",           ctypes.c_uint64),
        ("len",              ctypes.c_uint32),
        ("speed_hz",         ctypes.c_uint32),
        ("delay_usecs",      ctypes.c_uint16),
        ("bits_per_word",    ctypes.c_uint8),
        ("cs_change",        ctypes.c_uint8),
        ("tx_nbits",         ctypes.c_uint8),
        ("rx_nbits",         ctypes.c_uint8),
        ("word_delay_usecs", ctypes.c_uint8),
        ("pad",              ctypes.c_uint8),
    ]

def _spi_msg(n):
    sz = ctypes.sizeof(SpiIocTransfer) * n
    return (0x40000000 | ((sz & 0x3FFF) << 16) | (0x6b << 8) | 0)

SPI_IOC_MESSAGE_1 = _spi_msg(1)

# ──────────────────────────────────────────────────────────────────────────────
# HARDWARE BRIDGE
# ──────────────────────────────────────────────────────────────────────────────
class HWBridge:

    def __init__(self):
        self.libc   = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.fd_adc = self._open_spi(ADS7841_DEVICE)
        self.fd_dac = self._open_spi(LTC2604_DEVICE)
        self.memfd  = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self._map_fpga()
        self.phaco_off()
        print("[INIT] Hardware ready.")

    # ── SPI ──────────────────────────────────────────────────────────────────
    def _open_spi(self, dev):
        try:
            fd = os.open(dev, os.O_RDWR)
            self.libc.ioctl(fd, SPI_IOC_WR_MODE,
                            ctypes.byref(ctypes.c_uint8(SPI_MODE)))
            self.libc.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD,
                            ctypes.byref(ctypes.c_uint8(SPI_BITS)))
            self.libc.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ,
                            ctypes.byref(ctypes.c_uint32(SPI_SPEED)))
            m = ctypes.c_uint8(0);  b = ctypes.c_uint8(0);  s = ctypes.c_uint32(0)
            self.libc.ioctl(fd, 0x40016b02, ctypes.byref(m))
            self.libc.ioctl(fd, 0x40016b04, ctypes.byref(b))
            self.libc.ioctl(fd, 0x40046b05, ctypes.byref(s))
            print(f"[SPI OK] {dev}  mode={m.value}  bits={b.value}  speed={s.value}")
            return fd
        except Exception as e:
            print(f"[SPI FAIL] {dev}: {e}")
            return -1

    def spi_read(self, cmd):
        """Read one 12-bit sample from ADS7841."""
        tx = (ctypes.c_uint8 * 3)(cmd, 0x00, 0x00)
        rx = (ctypes.c_uint8 * 3)()
        tr = SpiIocTransfer()
        tr.tx_buf        = ctypes.cast(tx, ctypes.c_void_p).value
        tr.rx_buf        = ctypes.cast(rx, ctypes.c_void_p).value
        tr.len           = 3
        tr.speed_hz      = SPI_SPEED
        tr.bits_per_word = SPI_BITS
        tr.cs_change     = 0
        self.libc.ioctl(self.fd_adc, SPI_IOC_MESSAGE_1, ctypes.byref(tr))
        raw = (rx[1] << 8) | rx[2]
        return max(0, min(raw >> 3, ADC_FULLSCALE))

    # ── FPGA mmap ─────────────────────────────────────────────────────────────
    def _map_fpga(self):
        page   = XPAR_BASE & ~MAP_MASK
        offset = XPAR_BASE &  MAP_MASK
        self.libc.mmap.restype = ctypes.c_void_p
        mapped = self.libc.mmap(None, MAP_SIZE, 3, 1, self.memfd, page)
        self.base = mapped + offset
        print("[FPGA] mmap OK")

    def write_reg(self, off, val):
        ptr = ctypes.cast(self.base + off, ctypes.POINTER(ctypes.c_uint16))
        ptr[0] = val & 0xFFFF

    # ── DAC / Phaco ───────────────────────────────────────────────────────────
    def write_dac(self, val):
        os.write(self.fd_dac,
                 bytes([0x00, 0x30, (val >> 8) & 0xFF, val & 0xFF]))

    def phaco_power(self, percent):
        pct     = max(0, min(100, percent))
        dac_val = int(39321.0 + pct * 249.03)
        self.write_dac(min(dac_val, 64224))

    def emit_tune_start(self):
        self.write_reg(REG_TUNE_REQ, TUNE_REQUEST_MASK)

    def emit_tune_stop(self):
        self.write_reg(REG_TUNE_REQ, 0x0000)

    def freq_count(self, cnt):
        self.write_reg(REG_FREQ_COUNT, cnt)

    def phaco_off(self):
        self.write_reg(REG_PHACO_ONOFF, 0x0000)
        self.write_reg(REG_FREQ_COUNT,  0x0000)

    # ── SWEEP ─────────────────────────────────────────────────────────────────
    def sweep(self, min_freq_khz=38.0, max_freq_khz=46.0,
              progress_cb=None, stop_event=None):
        count_high = int(100000.0 / min_freq_khz)
        count_low  = int(100000.0 / max_freq_khz)
        print(f"[SWEEP] countHigh={count_high}  countLow={count_low}")

        self.write_reg(REG_FS_COUNT,    count_high)
        self.write_reg(REG_PULSE_COUNT, 500)
        self.write_reg(REG_PDM_MODE,    PDM_CONTINUOUS)
        self.write_reg(REG_PHACO_ONOFF, ((count_high << 1) & 0xFFFF) | 0x01)
        self.emit_tune_start()
        time.sleep(10_000e-6)

        os.makedirs("/home/sweep", exist_ok=True)
        ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = f"/home/sweep/sweep_{ts}.txt"

        samples = []
        cnt     = count_high

        with open(log_path, "w") as log:
            log.write("Freq_kHz\tCH2_0xA7_avg\tCH3_0xE7_avg\n")

            while cnt >= count_low:
                if stop_event and stop_event.is_set():
                    break

                self.freq_count(cnt)
                time.sleep(300e-6)

                raw_A7 = []
                raw_E7 = []

                for _ in range(SAMPLES_PER_STEP):
                    raw_A7.append(self.spi_read(CH_0xA7))
                    raw_E7.append(self.spi_read(CH_0xE7))

                avg_A7 = sum(raw_A7) / SAMPLES_PER_STEP
                avg_E7 = sum(raw_E7) / SAMPLES_PER_STEP

                freq_khz = 100_000.0 / cnt

                sample = {
                    'freq' : freq_khz,
                    '0xA7' : avg_A7,
                    '0xE7' : avg_E7,
                }
                samples.append(sample)

                log.write(
                    f"{freq_khz:.4f}\t"
                    f"{avg_A7:.2f}\t{avg_E7:.2f}\n"
                )

                if progress_cb:
                    progress_cb(freq_khz, int(avg_A7), int(avg_E7))

                print(f"cnt={cnt:4d}  freq={freq_khz:.3f} kHz  "
                      f"0xA7={avg_A7:.1f}  0xE7={avg_E7:.1f}")

                cnt -= 2

        self.emit_tune_stop()
        self.phaco_off()

        print(f"[SWEEP] done — {len(samples)} steps — log: {log_path}")
        return samples, log_path

    def destroy(self):
        try:
            self.phaco_off()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# KEYPAD DIALOG
# ──────────────────────────────────────────────────────────────────────────────
class KeypadDialog(QDialog):

    STYLE = """
        QDialog {
            background: #1e1e2e;
            border: 2px solid #6a4a8a;
            border-radius: 10px;
        }
        QLabel#title {
            color: #ffffff;
            font-size: 18px;
            font-weight: bold;
        }
        QLabel#display {
            background: #0d0d1a;
            color: #ffdd00;
            border: 2px solid #8a6aaa;
            border-radius: 6px;
            font-size: 28px;
            font-weight: bold;
            padding: 8px 14px;
            min-height: 52px;
        }
        QPushButton {
            background: #4a3a6a;
            color: #ffffff;
            border: 1px solid #8a6aaa;
            border-radius: 8px;
            font-size: 22px;
            font-weight: bold;
            min-width: 82px;
            min-height: 68px;
        }
        QPushButton:pressed  { background: #7a5a9a; }
        QPushButton#btnOk  {
            background: #1a8a1a; border-color: #2acc2a;
            font-size: 18px; font-weight: bold; min-height: 58px;
        }
        QPushButton#btnOk:pressed  { background: #2aaa2a; }
        QPushButton#btnCan {
            background: #2255cc; border-color: #4488ff;
            font-size: 18px; font-weight: bold; min-height: 58px;
        }
        QPushButton#btnCan:pressed { background: #3366dd; }
        QPushButton#btnBack {
            background: #3355cc; border-color: #5577ee;
        }
    """

    def __init__(self, title="Enter value", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet(self.STYLE)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self._buf = ""
        self._build(title)

    def _build(self, title):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(24, 24, 24, 24)

        lbl = QLabel(title); lbl.setObjectName("title")
        lbl.setAlignment(Qt.AlignCenter)
        root.addWidget(lbl)

        self.display = QLabel("_"); self.display.setObjectName("display")
        self.display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.display.setMinimumWidth(320)
        root.addWidget(self.display)

        grid = QGridLayout(); grid.setSpacing(10)
        for txt, r, c in [
            ("7",0,0),("8",0,1),("9",0,2),
            ("4",1,0),("5",1,1),("6",1,2),
            ("1",2,0),("2",2,1),("3",2,2),
            ("0",3,0),(".",3,1),
        ]:
            b = QPushButton(txt)
            b.clicked.connect(lambda _, t=txt: self._key(t))
            grid.addWidget(b, r, c)

        btn_back = QPushButton("⌫"); btn_back.setObjectName("btnBack")
        btn_back.clicked.connect(self._back)
        grid.addWidget(btn_back, 3, 2)
        root.addLayout(grid)

        row = QHBoxLayout(); row.setSpacing(12)
        btn_can = QPushButton("Cancel"); btn_can.setObjectName("btnCan")
        btn_can.clicked.connect(self.reject); row.addWidget(btn_can)
        btn_ok  = QPushButton("OK");     btn_ok.setObjectName("btnOk")
        btn_ok.clicked.connect(self._ok); row.addWidget(btn_ok)
        root.addLayout(row)

    def _key(self, ch):
        if ch == "." and "." in self._buf: return
        self._buf += ch; self.display.setText(self._buf)

    def _back(self):
        self._buf = self._buf[:-1]
        self.display.setText(self._buf or "_")

    def _ok(self):
        try:
            float(self._buf); self.accept()
        except ValueError:
            self.display.setText("invalid!")

    def getValue(self): return self._buf


# ──────────────────────────────────────────────────────────────────────────────
# PLOT WIDGET
# ──────────────────────────────────────────────────────────────────────────────
class PlotWidget(QWidget):
    """
    Dark-canvas dual-curve plot matching physical display style.

    ╔══════════════════════════════════════════════════════╗
    ║  COLOR MAP  (matches the physical device screen)     ║
    ║  CH2 (0xA7) — Phaco current  →  ORANGE / YELLOW     ║
    ║  CH3 (0xE7) — Voltage        →  BLUE                ║
    ║  Peak A7 marker              →  GREEN dashed         ║
    ╚══════════════════════════════════════════════════════╝

    X-AXIS LOCK:
    The x-axis range is set ONCE from _min_freq / _max_freq at sweep
    start and is NEVER recalculated from the live data — this prevents
    the graph from auto-zooming in as data arrives or after completion.
    """

    # ── FIX: corrected colours to match physical display ──────────────────────
    COLOR_CH2  = "#ffaa00"   # orange-yellow  — CH2 Phaco current (0xA7)
    COLOR_CH3  = "#0088ff"   # blue           — CH3 Voltage       (0xE7)
    COLOR_PEAK = "#00dd00"   # green dashed   — peak marker

    def __init__(self):
        super().__init__()
        
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)
        
        self._xs     = []
        self._ys_A7  = []
        self._ys_E7  = []
        # ── FIX: range is set explicitly; never derived from data ─────────────
        self._x_min  = 38.0
        self._x_max  = 46.0
        self._peak_A7_freq = None
        self._buf    = None
        self._dirty  = True

    # ── public API ────────────────────────────────────────────────────────────
    def set_x_range(self, xmin, xmax):
        """Lock the displayed x-axis to [xmin, xmax]. Never auto-scales."""
        if xmin >= xmax:
            return
        self._x_min = xmin
        self._x_max = xmax
        self._dirty = True
        self.update()

    def set_data(self, xs, ys_A7, ys_E7, peak_A7_freq=None):
        """
        Update plotted data.
        The x-axis range is NOT touched here — it stays at whatever
        set_x_range() last set it to, so the view never zooms in.
        """
        self._xs           = list(xs)
        self._ys_A7        = list(ys_A7)
        self._ys_E7        = list(ys_E7)
        self._peak_A7_freq = peak_A7_freq
        self._dirty = True
        self.update()

    def clear(self):
        self._xs = []; self._ys_A7 = []; self._ys_E7 = []
        self._peak_A7_freq = None
        self._dirty = True
        self.update()

    def resizeEvent(self, e):
        self._dirty = True
        super().resizeEvent(e)

    # ── render ────────────────────────────────────────────────────────────────
    def _redraw(self):
        W, H = self.width(), self.height()

        # ── FIXED margins — legend now sits INSIDE MB so nothing overflows ─────
        # ML=72 (Y labels), MR=20, MT=40 (title), MB=56 (x labels+legend)
        # These are CONSTANT — they never depend on data or widget state.
        ML, MR, MT, MB = 72, 20, 40, 56

        self._buf = QPixmap(W, H)
        p = QPainter(self._buf)
        p.setRenderHint(QPainter.Antialiasing)

        # ── backgrounds ───────────────────────────────────────────────────────
        # Outer widget: same purple as reference image (#3d1f4e approx)
        BG_OUTER  = QColor("#3b1f4a")   # purple outer  — matches Image 2 border
        BG_CANVAS = QColor("#4a2060")   # purple canvas — matches Image 2 plot area
        p.fillRect(0, 0, W, H, BG_OUTER)

        rx, ry = ML, MT
        rw = max(W - ML - MR, 1)
        rh = max(H - MT - MB, 1)
        p.fillRect(rx, ry, rw, rh, BG_CANVAS)   # ← purple plot canvas

        # ── axis limits — x ALWAYS from stored range, never from data ─────────
        ymin, ymax = 0.0, float(ADC_FULLSCALE)
        xmin, xmax = self._x_min, self._x_max    # LOCKED — set_x_range only
        NUM_X = 9

        def yp(v):
            return ry + rh - int((v - ymin) / (ymax - ymin) * rh)

        def xp(v):
            if xmax == xmin: return rx
            return rx + int((v - xmin) / (xmax - xmin) * rw)

        # ── grid (white semi-transparent lines like Image 2) ──────────────────
        grid_pen = QPen(QColor(255, 255, 255, 60), 1, Qt.SolidLine)
        p.setPen(grid_pen)
        for yt in Y_TICKS:
            p.drawLine(rx, yp(yt), rx + rw, yp(yt))
        for i in range(NUM_X):
            gx = rx + int(i * rw / (NUM_X - 1))
            p.drawLine(gx, ry, gx, ry + rh)

        # canvas border
        p.setPen(QPen(QColor(255, 255, 255, 80), 1))
        p.drawRect(rx, ry, rw, rh)

        # ── fonts ─────────────────────────────────────────────────────────────
        f_tick  = QFont("Sans", 10)
        f_axis  = QFont("Sans", 10); f_axis.setBold(True)
        f_title = QFont("Sans", 12); f_title.setBold(True)
        f_leg   = QFont("Sans",  9)
        f_peak  = QFont("Sans",  9); f_peak.setBold(True)

        TEXT_COL = QColor("#ffffff")   # all axis text white on purple

        # ── Y tick labels ──────────────────────────────────────────────────────
        p.setFont(f_tick); p.setPen(TEXT_COL)
        fm = p.fontMetrics()
        for yt in Y_TICKS:
            lbl = str(yt)
            p.drawText(rx - fm.horizontalAdvance(lbl) - 5, yp(yt) + fm.ascent() // 2, lbl)

        # ── X tick labels — placed at fixed pixel row INSIDE MB ────────────────
        p.setFont(f_tick); p.setPen(TEXT_COL)
        fm = p.fontMetrics()
        x_lbl_y = ry + rh + fm.height() + 2          # row 1 below canvas
        for i in range(NUM_X):
            val = xmin + (xmax - xmin) * i / (NUM_X - 1)
            gx  = rx + int(i * rw / (NUM_X - 1))
            lbl = f"{val:.1f}"
            p.drawText(gx - fm.horizontalAdvance(lbl) // 2, x_lbl_y, lbl)

        # ── Y axis title (rotated) ─────────────────────────────────────────────
        p.setFont(f_axis); p.setPen(TEXT_COL)
        p.save()
        p.translate(12, ry + rh // 2)
        p.rotate(-90)
        lbl_y = "ADC Counts"
        fm    = p.fontMetrics()
        p.drawText(-fm.width(lbl_y) // 2, fm.ascent() - 2, lbl_y)
        p.restore()

        # ── X axis title — row 2 below canvas, still within MB ────────────────
        p.setFont(f_axis); p.setPen(TEXT_COL)
        fm    = p.fontMetrics()
        lbl_x = "Freq (kHz)"
        x_title_y = x_lbl_y + fm.height() + 1
        p.drawText(rx + rw // 2 - fm.width(lbl_x) // 2, x_title_y, lbl_x)

        # ── plot title (inside MT area above canvas) ───────────────────────────
        p.setFont(f_title); p.setPen(TEXT_COL)
        fm    = p.fontMetrics()
        lbl_t = "Frequency Sweep — CH2 (Current)  CH3 (Voltage)"
        p.drawText(rx + rw // 2 - fm.width(lbl_t) // 2, MT - 8, lbl_t)

        # ── legend — drawn INSIDE the top-right of the canvas so it can't push
        #    the widget boundary and cause a zoom/resize ──────────────────────
        p.setClipRect(rx, ry, rw, rh)
        p.setFont(f_leg); fm = p.fontMetrics()
        swatch, gap = 24, 5
        lbl2 = "CH2  0xA7  Phaco (blue)"
        lbl3 = "CH3  0xE7  Voltage (orange)"
        leg_x  = rx + rw - 260 - 10
        leg_y2 = ry + fm.height() + 8
        leg_y3 = leg_y2 + fm.height() + 4

        # shadow box behind legend text
        box_w = 260
        box_h = fm.height() * 2 + fm.height() + 6
        p.fillRect(leg_x - 6, leg_y2 - fm.height(), box_w, box_h,
                   QColor(0, 0, 0, 100))

        p.setPen(QPen(QColor(self.COLOR_CH2), 3))
        p.drawLine(leg_x, leg_y2 - 4, leg_x + swatch, leg_y2 - 4)
        p.setPen(TEXT_COL)
        p.drawText(leg_x + swatch + gap, leg_y2, lbl2)

        p.setPen(QPen(QColor(self.COLOR_CH3), 3))
        p.drawLine(leg_x, leg_y3 - 4, leg_x + swatch, leg_y3 - 4)
        p.setPen(TEXT_COL)
        p.drawText(leg_x + swatch + gap, leg_y3, lbl3)

        p.setClipping(False)

        # ── peak marker (green dashed vertical line) ───────────────────────────
        if self._peak_A7_freq is not None:
            xpk = xp(self._peak_A7_freq)
            if rx < xpk < (rx + rw):
                p.setClipRect(rx, ry, rw, rh)
                p.setPen(QPen(QColor(self.COLOR_PEAK), 2, Qt.DashLine))
                p.drawLine(xpk, ry, xpk, ry + rh)
                p.setClipping(False)
                p.setFont(f_peak)
                p.setPen(QColor(self.COLOR_PEAK))
                p.drawText(xpk + 4, ry + 18, "Peak")

        # ── curves ────────────────────────────────────────────────────────────
        p.setClipRect(rx, ry, rw, rh)

        def draw_curve(xs, ys, color, width=2.0):
            if len(xs) < 2: return
            pen = QPen(QColor(color), width)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            for i in range(len(xs) - 1):
                p.drawLine(xp(xs[i]), yp(ys[i]), xp(xs[i+1]), yp(ys[i+1]))

        draw_curve(self._xs, self._ys_E7, self.COLOR_CH3, 2.0)   # E7 blue   — behind
        draw_curve(self._xs, self._ys_A7, self.COLOR_CH2, 2.0)   # A7 orange — on top

        p.setClipping(False)
        p.end()
        self._dirty = False

    def paintEvent(self, e):
        if self._buf is None or self._dirty:
            self._redraw()
        QPainter(self).drawPixmap(0, 0, self._buf)


# ──────────────────────────────────────────────────────────────────────────────
# SIGNAL BRIDGE
# ──────────────────────────────────────────────────────────────────────────────
class SweepBridge(QObject):
    done     = pyqtSignal(object)            # (samples, log_path)
    progress = pyqtSignal(float, int, int)   # freq_khz, ch2, ch3


# ──────────────────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ──────────────────────────────────────────────────────────────────────────────
class Main(QMainWindow):

    SS = """
        QMainWindow, QWidget#central { background:#1a1a2e; color:#ffffff; }
        QWidget#topbar    { background:#0d1117; border-bottom:1px solid #333355; }
        QWidget#bottombar { background:#0d1117; border-top:1px solid #333355; }
        QWidget#infobar   { background:#111122; border-top:1px solid #333333; }

        QLabel            { font-size:14px; color:#cccccc; }
        QLabel#lbl_status { color:#00ee00; font-size:15px; font-weight:bold; }
        QLabel#lbl_peak_A7{ color:#ffaa00; font-size:14px; font-weight:bold; }
        QLabel#lbl_peak_E7{ color:#55aaff; font-size:14px; font-weight:bold; }
        QLabel#lbl_info1,
        QLabel#lbl_info2,
        QLabel#lbl_info3  { color:#dddddd; font-size:13px; padding-left:6px; }
        QLabel#ctrl_lbl   { color:#ffffff; font-size:14px; font-weight:bold; }

        QPushButton {
            background:#3a2a5a; color:#ffffff;
            border:1px solid #6a4a8a; border-radius:5px;
            padding:6px 14px; font-size:14px; font-weight:bold;
        }
        QPushButton:pressed  { background:#5a3a7a; }
        QPushButton:disabled { background:#1a1a2a; color:#555555; }

        QPushButton#btnSweep {
            background:#1a7a1a; border:2px solid #2aaa2a;
            border-radius:6px; padding:8px 20px;
            font-size:15px; font-weight:bold; color:#ffffff;
        }
        QPushButton#btnSweep:pressed  { background:#2a9a2a; }
        QPushButton#btnSweep:disabled { background:#0a3a0a; color:#555555; }
        QPushButton#btnClear {
            background:#3a2a5a; border:1px solid #6a4a8a;
            font-size:13px; padding:6px 12px; color:#ffffff;
        }

        QLineEdit {
            background:#2a1a3a; color:#ffffff;
            border:2px solid #6a4a8a; border-radius:4px;
            padding:5px 8px; font-size:15px; font-weight:bold;
            min-width:70px;
        }
    """

    def __init__(self):
        super().__init__()
        self.hw            = HWBridge()
        self.sweep_running = False
        self.bridge        = SweepBridge()
        self.bridge.done.connect(self._on_sweep_done)
        self.bridge.progress.connect(self._on_progress)

        self._live_xs  = []
        self._live_A7  = []
        self._live_E7  = []
        self._peak_A7_val  = 0.0;  self._peak_A7_freq = 0.0
        self._peak_E7_val  = 0.0;  self._peak_E7_freq = 0.0
        self._n_samples    = 0
        self._last_log     = ""

        self._min_freq = 38.0
        self._max_freq = 46.0

        self.setWindowTitle("Phaco Tuning  v4")
        self.setStyleSheet(self.SS)
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget(); central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── top bar ───────────────────────────────────────────────────────────
        topbar = QWidget(); topbar.setObjectName("topbar")
        topbar.setFixedHeight(34)
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(8, 2, 8, 2); tl.setSpacing(6)

        self.lbl_status = QLabel("Hardware ready")
        self.lbl_status.setObjectName("lbl_status")
        tl.addWidget(self.lbl_status)
        tl.addStretch()

        # ── FIX: peak labels use matching colours (orange / blue) ─────────────
        self.lbl_peak_A7 = QLabel("Peak CH2 (0xA7): --")
        self.lbl_peak_A7.setObjectName("lbl_peak_A7")   # styled orange in SS
        tl.addWidget(self.lbl_peak_A7)

        sep = QLabel(" | "); sep.setStyleSheet("color:#555577;")
        tl.addWidget(sep)

        self.lbl_peak_E7 = QLabel("Peak CH3 (0xE7): --")
        self.lbl_peak_E7.setObjectName("lbl_peak_E7")   # styled blue in SS
        tl.addWidget(self.lbl_peak_E7)

        root.addWidget(topbar)

        # ── plot ──────────────────────────────────────────────────────────────
        self.plot = PlotWidget()
        # ── FIX: set x range on plot widget at startup so axis is always locked
        self.plot.set_x_range(self._min_freq, self._max_freq)
        root.addWidget(self.plot, stretch=1)

        # ── info bar ──────────────────────────────────────────────────────────
        infobar = QWidget(); infobar.setObjectName("infobar")
        infobar.setFixedHeight(56)
        il = QVBoxLayout(infobar)
        il.setContentsMargins(8, 2, 8, 2); il.setSpacing(1)

        self.lbl_info1 = QLabel("Saved — (no sweep yet)")
        self.lbl_info1.setObjectName("lbl_info1")
        il.addWidget(self.lbl_info1)

        self.lbl_info2 = QLabel("Peak Impedance (0xE7): --")
        self.lbl_info2.setObjectName("lbl_info2")
        il.addWidget(self.lbl_info2)

        self.lbl_info3 = QLabel("Peak Current  (0xA7): --")
        self.lbl_info3.setObjectName("lbl_info3")
        il.addWidget(self.lbl_info3)

        root.addWidget(infobar)

        # ── bottom control bar ────────────────────────────────────────────────
        bottombar = QWidget(); bottombar.setObjectName("bottombar")
        bottombar.setFixedHeight(46)
        bl = QHBoxLayout(bottombar)
        bl.setContentsMargins(8, 4, 8, 4); bl.setSpacing(8)

        for lbl_txt, attr in [("Min (kHz):", "edit_min"), ("Max (kHz):", "edit_max")]:
            lbl = QLabel(lbl_txt); lbl.setObjectName("ctrl_lbl")
            bl.addWidget(lbl)
            default = f"{self._min_freq:.1f}" if "Min" in lbl_txt else f"{self._max_freq:.1f}"
            edit = QLineEdit(default); edit.setReadOnly(True)
            target = "min" if "Min" in lbl_txt else "max"
            edit.mousePressEvent = (lambda e, t=target: self._open_keypad(t))
            setattr(self, attr, edit)
            bl.addWidget(edit)

        btn_apply = QPushButton("Apply Range")
        btn_apply.clicked.connect(self._apply_range)
        bl.addWidget(btn_apply)

        bl.addSpacing(10)

        self.btn_sweep = QPushButton("▶  Start Sweep")
        self.btn_sweep.setObjectName("btnSweep")
        self.btn_sweep.clicked.connect(self._start_sweep)
        bl.addWidget(self.btn_sweep)

        btn_clear = QPushButton("Clear Graph")
        btn_clear.setObjectName("btnClear")
        btn_clear.clicked.connect(self._clear)
        bl.addWidget(btn_clear)

        bl.addStretch()
        root.addWidget(bottombar)

    # ── keypad ────────────────────────────────────────────────────────────────
    def _open_keypad(self, target):
        dlg = KeypadDialog(
            "Min Frequency (kHz)" if target == "min" else "Max Frequency (kHz)",
            self)
        if dlg.exec_() == QDialog.Accepted:
            try:
                v = float(dlg.getValue())
                if v <= 0: raise ValueError
                if target == "min":
                    self._min_freq = v; self.edit_min.setText(f"{v:.1f}")
                else:
                    self._max_freq = v; self.edit_max.setText(f"{v:.1f}")
                if self._min_freq < self._max_freq:
                    # ── FIX: always lock the axis to user-set range ───────────
                    self.plot.set_x_range(self._min_freq, self._max_freq)
            except ValueError:
                self._status("Invalid value", "#ff4444")

    def _apply_range(self):
        try:
            xmin = float(self.edit_min.text())
            xmax = float(self.edit_max.text())
            if xmin >= xmax: raise ValueError
        except ValueError:
            self._status("Invalid range — min must be < max", "#ff4444")
            return
        self._min_freq = xmin; self._max_freq = xmax
        # ── FIX: lock the plot axis immediately on Apply ───────────────────────
        self.plot.set_x_range(xmin, xmax)
        self._status(f"Range set: {xmin} – {xmax} kHz", "#00ee00")

    # ── sweep ─────────────────────────────────────────────────────────────────
    def _start_sweep(self):
        if self.sweep_running: return
        if self._min_freq <= 0 or self._max_freq <= self._min_freq:
            self._status("Invalid frequency range", "#ff4444"); return

        self.sweep_running = True
        self.btn_sweep.setEnabled(False)
        self._status("Sweeping…", "#ffbb00")

        # ── FIX: lock x range BEFORE sweep starts so axis never moves ─────────
        self.plot.set_x_range(self._min_freq, self._max_freq)

        self._live_xs  = []; self._live_A7 = []; self._live_E7 = []
        self._peak_A7_val = 0.0; self._peak_A7_freq = 0.0
        self._peak_E7_val = 0.0; self._peak_E7_freq = 0.0
        self._n_samples   = 0

        self.lbl_info1.setText("Sweeping — please wait…")
        self.lbl_info2.setText("Peak Impedance (0xE7): --")
        self.lbl_info3.setText("Peak Current  (0xA7): --")

        min_f = self._min_freq; max_f = self._max_freq

        def _cb(f, a7, e7):
            self.bridge.progress.emit(f, a7, e7)

        def work():
            try:
                samples, log_path = self.hw.sweep(
                    min_f, max_f, progress_cb=_cb)
            except Exception as ex:
                samples, log_path = [], ""
                print(f"[SWEEP ERROR] {ex}")
            self.bridge.done.emit((samples, log_path))

        threading.Thread(target=work, daemon=True).start()

    @pyqtSlot(float, int, int)
    def _on_progress(self, freq, a7, e7):
        self._live_xs.append(freq)
        self._live_A7.append(a7)
        self._live_E7.append(e7)
        self._n_samples += 1

        if a7 > self._peak_A7_val:
            self._peak_A7_val = a7; self._peak_A7_freq = freq
        if e7 > self._peak_E7_val:
            self._peak_E7_val = e7; self._peak_E7_freq = freq

        # ── FIX: set_data ONLY — never call set_x_range here ──────────────────
        # The x range is already locked; passing it again would not change it,
        # but keeping this slot clean makes the intent unambiguous.
        self.plot.set_data(self._live_xs, self._live_A7, self._live_E7,
                           peak_A7_freq=self._peak_A7_freq)

        self._status(f"Sweeping…  {freq:.2f} kHz  |  "
                     f"0xA7={a7}  0xE7={e7}", "#ffbb00")

    @pyqtSlot(object)
    def _on_sweep_done(self, payload):
        samples, log_path = payload
        self._last_log = log_path

        if samples:
            # ── FIX: set_data ONLY — x range already locked ───────────────────
            self.plot.set_data(self._live_xs, self._live_A7, self._live_E7,
                               peak_A7_freq=self._peak_A7_freq)

            # top-bar peak labels (orange for CH2, blue label for CH3)
            self.lbl_peak_A7.setText(
                f"Peak CH2: {self._peak_A7_freq:.2f} kHz "
                f"({int(self._peak_A7_val)} cts)")
            self.lbl_peak_E7.setText(
                f"Peak CH3: {self._peak_E7_freq:.2f} kHz "
                f"({int(self._peak_E7_val)} cts)")

            self._status(
                f"Sweep done  |  "
                f"CH2 peak @ {self._peak_A7_freq:.2f} kHz  |  "
                f"CH3 peak @ {self._peak_E7_freq:.2f} kHz", "#00ee00")

            self.lbl_info1.setText(
                f"Saved {self._n_samples} points  →  {log_path}")
            if self._peak_A7_val > 0:
                z = self._peak_E7_val / self._peak_A7_val
                self.lbl_info2.setText(
                    f"Peak Impedance (0xE7): {self._peak_E7_freq:.2f} kHz  "
                    f"(Z = {z:.3f})")
            else:
                self.lbl_info2.setText(
                    f"Peak Impedance (0xE7): {self._peak_E7_freq:.2f} kHz")
            self.lbl_info3.setText(
                f"Peak Current  (0xA7): {self._peak_A7_freq:.2f} kHz  "
                f"({int(self._peak_A7_val)} cts)")
        else:
            self._status("Sweep returned no data", "#ff4444")

        self.sweep_running = False
        self.btn_sweep.setEnabled(True)

    def _clear(self):
        self.plot.clear()
        # ── FIX: restore x-range after clear so axis stays locked ────────────
        self.plot.set_x_range(self._min_freq, self._max_freq)
        self.lbl_peak_A7.setText("Peak CH2 (0xA7): --")
        self.lbl_peak_E7.setText("Peak CH3 (0xE7): --")
        self.lbl_info1.setText("Saved — (no sweep yet)")
        self.lbl_info2.setText("Peak Impedance (0xE7): --")
        self.lbl_info3.setText("Peak Current  (0xA7): --")
        self._status("Graph cleared", "#00ee00")

    def _status(self, msg, color="#00ee00"):
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(
            f"color:{color}; font-size:9px; font-weight:bold;")

    def closeEvent(self, e):
        try: self.hw.destroy()
        except Exception: pass
        e.accept()


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    w   = Main()
    w.showFullScreen()
    app.processEvents()

    def _hw_init():
        try:
            w.hw.phaco_off()
            print("[HW] init done", flush=True)
        except Exception as e:
            print(f"[HW] init error: {e}", flush=True)

    QTimer.singleShot(500, _hw_init)
    sys.exit(app.exec_())
