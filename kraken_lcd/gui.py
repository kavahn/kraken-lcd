"""Kraken LCD Settings: a small window that edits config.toml.

It never talks to the cooler and nothing keeps running after you close it.
The kraken-lcd service notices the saved file and updates the screen.
"""

import os
import subprocess
import sys

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QIcon, QImage, QMovie, QPainter, QPainterPath, QPen, QPixmap, QRegion)
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QColorDialog, QComboBox, QFileDialog,
                             QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                             QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QVBoxLayout,
                             QWidget)

from . import __version__, config, sensors
from .render import Renderer

PREVIEW = 360
CHEVRON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "chevron.svg").replace("\\", "/")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
PRESETS = [("Pink", "#ff5ca8"), ("Hot pink", "#ff2d95"), ("Lavender", "#c4b5fd"), ("Purple", "#a855f7"),
           ("Blue", "#60a5fa"), ("Cyan", "#22d3ee"), ("Green", "#4ade80"), ("Orange", "#fb923c"),
           ("Red", "#ef4444"), ("White", "#f5f5f5")]
TEXT_PRESETS = [("White", "#ffffff"), ("Soft white", "#e5e5ea"), ("Pale pink", "#ffd1e6"), ("Pale blue", "#dbeafe")]
SENSOR_LABELS = [("CPU", "cpu"), ("GPU", "gpu"), ("Liquid", "liquid")]

BG, CARD, CARD_BORDER, FIELD = "#0b0b10", "#14141b", "#22222c", "#1c1c25"
TEXT, MUTED, FAINT = "#f4f4f6", "#9a9aa8", "#5e5e6b"


def stylesheet(accent):
    a = QColor(accent)
    hover = a.lighter(115).name()
    soft = QColor(a.red(), a.green(), a.blue(), 46).name(QColor.NameFormat.HexArgb)
    on_accent = "#0b0b10" if a.lightness() > 150 else "#ffffff"
    return f"""
    * {{ font-size: 13px; color: {TEXT}; }}
    QMainWindow, #root {{ background: {BG}; }}
    QScrollArea, #scrollBody {{ background: transparent; border: none; }}
    #card {{ background: {CARD}; border: 1px solid {CARD_BORDER}; border-radius: 16px; }}
    #cardTitle {{ color: {MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1.6px; }}
    #fieldLabel {{ color: {MUTED}; }}
    #hint {{ color: {FAINT}; font-size: 12px; }}
    #title {{ font-size: 20px; font-weight: 600; }}
    #subtitle {{ color: {MUTED}; }}
    #fileName {{ font-weight: 600; }}
    #thumb {{ background: {FIELD}; border-radius: 12px; }}
    #dropZone {{ background: {FIELD}; border: 1px dashed {CARD_BORDER}; border-radius: 14px; }}
    #dropZone[drag="true"] {{ border: 1px dashed {accent}; background: {soft}; }}

    QPushButton {{ background: {FIELD}; border: 1px solid {CARD_BORDER}; border-radius: 10px;
                  padding: 7px 14px; }}
    QPushButton:hover {{ border-color: {FAINT}; }}
    QPushButton:disabled {{ color: {FAINT}; }}
    QPushButton#primary {{ background: {accent}; color: {on_accent}; border: none; font-weight: 600;
                          padding: 9px 22px; border-radius: 12px; }}
    QPushButton#primary:hover {{ background: {hover}; }}
    QPushButton#primary:disabled {{ background: {FIELD}; color: {FAINT}; }}
    QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; padding: 9px 14px; }}
    QPushButton#ghost:hover {{ color: {TEXT}; }}
    QPushButton#ghost:disabled {{ color: {FAINT}; }}

    #segment {{ background: {FIELD}; border-radius: 11px; }}
    #segment QPushButton {{ background: transparent; border: none; border-radius: 8px; padding: 6px 14px;
                           color: {MUTED}; }}
    #segment QPushButton:hover {{ color: {TEXT}; }}
    #segment QPushButton:checked {{ background: {soft}; color: {accent}; font-weight: 600; }}

    QComboBox, QSpinBox {{ background: {FIELD}; border: 1px solid {CARD_BORDER}; border-radius: 10px;
                          padding: 6px 10px; min-height: 18px; }}
    QComboBox:hover, QSpinBox:hover {{ border-color: {FAINT}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: url("{CHEVRON}"); width: 12px; height: 12px; margin-right: 8px; }}
    QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {CARD_BORDER}; border-radius: 8px;
                                   selection-background-color: {soft}; selection-color: {TEXT}; padding: 4px; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}

    QSlider::groove:horizontal {{ height: 4px; background: {CARD_BORDER}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: #ffffff; width: 16px; height: 16px; margin: -6px 0;
                                 border-radius: 8px; }}
    QSlider::groove:horizontal:disabled {{ background: {FIELD}; }}
    QSlider::sub-page:horizontal:disabled {{ background: {FAINT}; }}
    QSlider::handle:horizontal:disabled {{ background: {FAINT}; }}

    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px; }}
    QScrollBar::handle:vertical {{ background: {CARD_BORDER}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QToolTip {{ background: {CARD}; color: {TEXT}; border: 1px solid {CARD_BORDER}; padding: 4px 8px; }}
    """


def _pil_to_pixmap(img):
    img = img.convert("RGB")
    q = QImage(img.tobytes("raw", "RGB"), img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(q.copy())


def _rounded(pm, size, radius):
    pm = pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)
    p.drawPixmap((size - pm.width()) // 2, (size - pm.height()) // 2, pm)
    p.end()
    return out


# --------------------------------------------------------------------------- small widgets

class Switch(QAbstractButton):
    """iOS-style toggle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 22)
        self.accent = QColor("#ff5ca8")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.accent if self.isChecked() else QColor(CARD_BORDER))
        p.drawRoundedRect(QRectF(0, 0, 40, 22), 11, 11)
        p.setBrush(QColor("#ffffff"))
        x = 20 if self.isChecked() else 2
        p.drawEllipse(QRectF(x, 2, 18, 18))
        p.end()


class Swatch(QAbstractButton):
    """Round colour dot; a ring shows the selected one."""

    def __init__(self, name, color, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.setToolTip(f"{name}  {color}")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(30, 30)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isChecked() or self.underMouse():
            p.setPen(QPen(QColor("#ffffff") if self.isChecked() else QColor(FAINT), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(1, 1, 28, 28))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(QRectF(5, 5, 20, 20))
        p.end()

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)


class CustomSwatch(Swatch):
    """The last dot: opens a colour picker and shows the custom colour."""

    picked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("Custom colour", "#2a2a35", parent)
        self.setToolTip("Custom colour…")
        self.clicked.connect(self._pick)
        self.custom = None

    def _pick(self):
        c = QColorDialog.getColor(self.custom or QColor("#ff5ca8"), self.window(), "Pick a colour")
        if c.isValid():
            self.picked.emit(c.name())
        else:
            self.setChecked(self.custom is not None)

    def paintEvent(self, e):
        if self.custom is not None:
            self.color = self.custom
            super().paintEvent(e)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(FAINT), 1.5, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(5, 5, 20, 20))
        p.setPen(QPen(QColor(MUTED), 1.6))
        p.drawLine(15, 10, 15, 20)
        p.drawLine(10, 15, 20, 15)
        p.end()


class ColorRow(QWidget):
    """Preset swatches + a custom one. Emits the chosen hex colour."""

    changed = pyqtSignal(str)

    def __init__(self, presets):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.swatches = []
        for name, c in presets:
            s = Swatch(name, c)
            s.clicked.connect(lambda _, c=c: self._choose(c))
            self.group.addButton(s)
            self.swatches.append(s)
            lay.addWidget(s)
        self.custom = CustomSwatch()
        self.custom.picked.connect(self._choose)
        self.group.addButton(self.custom)
        lay.addWidget(self.custom)
        lay.addStretch(1)
        self.value = presets[0][1]

    def _choose(self, color):
        self.set_value(color)
        self.changed.emit(self.value)

    def set_value(self, color):
        self.value = QColor(color).name()
        for s in self.swatches:
            if s.color.name() == self.value:
                s.setChecked(True)
                self.custom.custom = None
                break
        else:
            self.custom.custom = QColor(self.value)
            self.custom.setToolTip(f"Custom  {self.value}  (click to change)")
            self.custom.setChecked(True)
        for b in self.group.buttons():
            b.update()


class Segmented(QFrame):
    """Pill-shaped single-choice control."""

    changed = pyqtSignal()

    def __init__(self, options):
        super().__init__()
        self.setObjectName("segment")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons = []
        for label, data in options:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setProperty("value", data)
            self.group.addButton(b)
            self.buttons.append(b)
            lay.addWidget(b)
        self.group.buttonClicked.connect(lambda *_: self.changed.emit())
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def value(self):
        b = self.group.checkedButton()
        return b.property("value") if b else None

    def set_value(self, data):
        for b in self.buttons:
            if b.property("value") == data:
                b.setChecked(True)
                return


class DropZone(QFrame):
    """Background picker: thumbnail, name, Change / Remove, and drag & drop."""

    picked = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(14)
        self.thumb = QLabel()
        self.thumb.setObjectName("thumb")
        self.thumb.setFixedSize(64, 64)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.thumb)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name = QLabel()
        self.name.setObjectName("fileName")
        self.detail = QLabel()
        self.detail.setObjectName("hint")
        text.addStretch(1)
        text.addWidget(self.name)
        text.addWidget(self.detail)
        text.addStretch(1)
        lay.addLayout(text, 1)
        self.change = QPushButton("Choose…")
        self.change.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change.clicked.connect(self._browse)
        self.remove = QPushButton("Remove")
        self.remove.setObjectName("ghost")
        self.remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove.clicked.connect(lambda: self.picked.emit(""))
        lay.addWidget(self.remove)
        lay.addWidget(self.change)
        self.path = ""

    def show_path(self, path, animated):
        self.path = path
        full = os.path.expanduser(path) if path else ""
        if full and os.path.exists(full):
            pm = QPixmap(full)
            self.thumb.setPixmap(_rounded(pm, 64, 12) if not pm.isNull() else QPixmap())
            self.name.setText(os.path.basename(full))
            kind = "Animated GIF" if animated else "Picture"
            self.detail.setText(f"{kind} · drop another file here to swap")
            self.remove.setVisible(True)
        else:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("—")
            self.name.setText("No picture" if not path else "File not found")
            self.detail.setText("Plain black · drop a picture or GIF here")
            self.remove.setVisible(bool(path))

    def _browse(self):
        start = os.path.dirname(os.path.expanduser(self.path)) if self.path else os.path.expanduser("~/Pictures")
        path, _ = QFileDialog.getOpenFileName(self, "Choose a picture or GIF", start,
                                              "Pictures and GIFs (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;All files (*)")
        if path:
            self.picked.emit(path)

    @staticmethod
    def _file(event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(IMAGE_EXT):
                return url.toLocalFile()
        return None

    def _set_drag(self, on):
        self.setProperty("drag", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if self._file(e):
            e.acceptProposedAction()
            self._set_drag(True)

    def dragLeaveEvent(self, e):
        self._set_drag(False)

    def dropEvent(self, e):
        self._set_drag(False)
        f = self._file(e)
        if f:
            self.picked.emit(f)


def card(title):
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 16, 20, 18)
    lay.setSpacing(12)
    t = QLabel(title.upper())
    t.setObjectName("cardTitle")
    lay.addWidget(t)
    grid = QGridLayout()
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(12)
    grid.setColumnStretch(1, 1)
    lay.addLayout(grid)
    return frame, grid


def field(grid, row, label, widget):
    lab = QLabel(label)
    lab.setObjectName("fieldLabel")
    grid.addWidget(lab, row, 0, Qt.AlignmentFlag.AlignVCenter)
    if isinstance(widget, QWidget):
        grid.addWidget(widget, row, 1)
    else:
        grid.addLayout(widget, row, 1)


# --------------------------------------------------------------------------- window

class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kraken LCD")
        self.setMinimumSize(1000, 600)
        self.setWindowIcon(QIcon.fromTheme("preferences-desktop-display"))
        self.saved = config.load()
        self.temps = sensors.read_all(self.saved)
        self._movie = None
        self._loading = True

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(28, 24, 20, 20)
        outer.setSpacing(28)

        # ---------------- left: header + preview
        left = QVBoxLayout()
        left.setSpacing(6)
        title = QLabel("Kraken LCD")
        title.setObjectName("title")
        sub = QLabel("Screen settings for NZXT Kraken coolers")
        sub.setObjectName("subtitle")
        left.addWidget(title)
        left.addWidget(sub)
        left.addSpacing(22)
        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW, PREVIEW)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMask(QRegion(0, 0, PREVIEW, PREVIEW, QRegion.RegionType.Ellipse))
        self.preview.setStyleSheet("background: black;")
        holder = QWidget()
        holder.setFixedSize(PREVIEW + 40, PREVIEW + 40)
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(20, 20, 20, 20)
        hl.addWidget(self.preview)
        self.glow = QGraphicsDropShadowEffect(blurRadius=60, xOffset=0, yOffset=0)
        holder.setGraphicsEffect(self.glow)
        left.addWidget(holder, 0, Qt.AlignmentFlag.AlignHCenter)
        left.addSpacing(8)
        self.preview_note = QLabel()
        self.preview_note.setObjectName("hint")
        self.preview_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left.addWidget(self.preview_note)
        left.addStretch(1)
        self.service_label = QLabel()
        self.service_label.setWordWrap(True)
        self.service_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.service_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.service_label.setMaximumWidth(PREVIEW + 40)
        left.addWidget(self.service_label, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addLayout(left)

        # ---------------- right: cards in a scroll area + footer
        right = QVBoxLayout()
        right.setSpacing(14)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("scrollBody")
        cards = QVBoxLayout(body)
        cards.setContentsMargins(0, 0, 8, 0)
        cards.setSpacing(14)
        scroll.setWidget(body)
        right.addWidget(scroll, 1)
        outer.addLayout(right, 1)

        c, g = card("Background")
        self.drop = DropZone()
        self.drop.picked.connect(self.set_image)
        g.addWidget(self.drop, 0, 0, 1, 2)
        self.style_seg = Segmented([("Duotone", "duotone"), ("Original colours", "original")])
        self.style_seg.changed.connect(self.changed)
        field(g, 1, "Look", self.style_seg)
        self.dim, self.dim_value = self._slider(0, 90, "%")
        field(g, 2, "Darken", self._with_value(self.dim, self.dim_value))
        cards.addWidget(c)

        c, g = card("Colours")
        self.accent = ColorRow(PRESETS)
        self.accent.changed.connect(self.accent_changed)
        field(g, 0, "Accent", self.accent)
        self.text_color = ColorRow(TEXT_PRESETS)
        self.text_color.changed.connect(self.changed)
        field(g, 1, "Numbers", self.text_color)
        cards.addWidget(c)

        c, g = card("Temperatures")
        self.main_seg = Segmented(SENSOR_LABELS)
        self.main_seg.changed.connect(self.changed)
        field(g, 0, "Big number", self.main_seg)
        under = QHBoxLayout()
        under.setSpacing(10)
        self.sub = []
        for _ in range(2):
            cb = QComboBox()
            cb.addItem("Nothing", None)
            for label, key in SENSOR_LABELS:
                cb.addItem(label, key)
            cb.currentIndexChanged.connect(self.changed)
            self.sub.append(cb)
            under.addWidget(cb, 1)
        field(g, 1, "Underneath", under)
        self.gpu_sensor = QComboBox()
        for label, key in (("Edge (what most apps show)", "edge"), ("Junction (hotspot)", "junction"),
                           ("Memory", "mem")):
            self.gpu_sensor.addItem(label, key)
        self.gpu_sensor.currentIndexChanged.connect(self.changed)
        field(g, 2, "AMD GPU sensor", self.gpu_sensor)
        cards.addWidget(c)

        c, g = card("Screen")
        self.rotation = Segmented([("Auto", None), ("0°", 0), ("90°", 90), ("180°", 180), ("270°", 270)])
        self.rotation.setToolTip("Auto keeps the cooler's own setting")
        self.rotation.changed.connect(self.changed)
        field(g, 0, "Rotation", self.rotation)
        brow = QHBoxLayout()
        brow.setSpacing(12)
        self.bright_on = Switch()
        self.bright_on.setToolTip("Off keeps the cooler's own brightness")
        self.bright, self.bright_value = self._slider(0, 100, "%")
        self.bright_on.toggled.connect(lambda on: (self.bright.setEnabled(on), self.changed()))
        brow.addWidget(self.bright_on)
        brow.addLayout(self._with_value(self.bright, self.bright_value), 1)
        field(g, 1, "Brightness", brow)
        self.gif_refresh = QSpinBox()
        self.gif_refresh.setRange(5, 300)
        self.gif_refresh.setSuffix("  seconds")
        self.gif_refresh.setFixedWidth(130)
        self.gif_refresh.valueChanged.connect(self.changed)
        gr = QHBoxLayout()
        gr.addWidget(self.gif_refresh)
        hint = QLabel("Each refresh restarts the animation")
        hint.setObjectName("hint")
        gr.addWidget(hint, 1)
        field(g, 2, "GIF refresh", gr)
        cards.addWidget(c)
        cards.addStretch(1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 8, 0)
        self.status = QLabel()
        self.status.setObjectName("hint")
        foot.addWidget(self.status, 1)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setObjectName("ghost")
        self.revert_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.revert_btn.clicked.connect(self.revert)
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.setShortcut("Ctrl+S")
        self.save_btn.clicked.connect(self.save)
        foot.addWidget(self.revert_btn)
        foot.addWidget(self.save_btn)
        right.addLayout(foot)

        self._debounce = QTimer(self, singleShot=True, interval=160)
        self._debounce.timeout.connect(self.update_preview)
        self._temps_timer = QTimer(self, interval=3000)
        self._temps_timer.timeout.connect(self.refresh_temps)
        self._temps_timer.start()

        self.load_into_widgets(self.saved)
        self._loading = False
        self.apply_theme()
        self.update_service_label()
        self.update_preview()
        self.update_buttons()

    # ---------------------------------------------------------------- helpers
    def _slider(self, lo, hi, unit):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QLabel()
        v.setObjectName("fieldLabel")
        v.setMinimumWidth(40)
        v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        s.valueChanged.connect(lambda x: (v.setText(f"{x}{unit}"), self.changed()))
        return s, v

    @staticmethod
    def _with_value(slider, label):
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(slider, 1)
        row.addWidget(label)
        return row

    @staticmethod
    def _select(combo, data):
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return

    def apply_theme(self):
        accent = self.accent.value
        QApplication.instance().setStyleSheet(stylesheet(accent))
        c = QColor(accent)
        c.setAlpha(120)
        self.glow.setColor(c)
        self.bright_on.accent = QColor(accent)
        self.bright_on.update()

    # ---------------------------------------------------------------- state <-> widgets
    def load_into_widgets(self, cfg):
        self._loading = True
        self.image = cfg.get("image", "")
        self.style_seg.set_value(cfg["style"])
        self.dim.setValue(round(float(cfg["dim"]) * 100))
        self.dim_value.setText(f"{self.dim.value()}%")
        self.accent.set_value(cfg["accent_color"])
        self.text_color.set_value(cfg["text_color"])
        show = cfg["show"]
        self.main_seg.set_value(show[0])
        for i, cb in enumerate(self.sub):
            self._select(cb, show[i + 1] if len(show) > i + 1 else None)
        self._select(self.gpu_sensor, cfg["gpu_sensor"])
        self.rotation.set_value(cfg["orientation"])
        b = cfg["brightness"]
        self.bright_on.setChecked(b is not None)
        self.bright.setEnabled(b is not None)
        self.bright.setValue(int(b) if b is not None else 50)
        self.bright_value.setText(f"{self.bright.value()}%")
        self.gif_refresh.setValue(int(cfg["gif_refresh"]))
        self._loading = False

    def current(self):
        cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.saved.items()}
        show = [self.main_seg.value() or "cpu"]
        for cb in self.sub:
            key = cb.currentData()
            if key and key not in show:
                show.append(key)
        cfg.update({
            "image": self.image,
            "style": self.style_seg.value() or "duotone",
            "dim": round(self.dim.value() / 100, 2),
            "accent_color": self.accent.value,
            "text_color": self.text_color.value,
            "show": show,
            "gpu_sensor": self.gpu_sensor.currentData(),
            "orientation": self.rotation.value(),
            "brightness": self.bright.value() if self.bright_on.isChecked() else None,
            "gif_refresh": float(self.gif_refresh.value()),
        })
        return cfg

    def dirty(self):
        return self.current() != self.saved

    # ---------------------------------------------------------------- actions
    def set_image(self, path):
        home = os.path.expanduser("~")
        if path.startswith(home + os.sep):
            path = "~" + path[len(home):]
        self.image = path
        self.changed()

    def accent_changed(self, *_):
        self.apply_theme()
        self.changed()

    def changed(self, *_):
        if self._loading:
            return
        self.update_buttons()
        self._debounce.start()

    def update_buttons(self):
        d = self.dirty()
        self.save_btn.setEnabled(d)
        self.revert_btn.setEnabled(d)
        self.status.setText("Unsaved changes" if d else self.status.text() if "Saved" in self.status.text() else "")

    def revert(self):
        self.load_into_widgets(self.saved)
        self.apply_theme()
        self.status.setText("")
        self.update_buttons()
        self.update_preview()

    def save(self):
        try:
            config.save(self.current())
        except Exception as exc:
            QMessageBox.critical(self, "Couldn't save", str(exc))
            return
        self.saved = config.load()
        self.status.setText("Saved · the screen updates in a few seconds")
        self.update_buttons()
        self.update_service_label()

    def refresh_temps(self):
        new = sensors.read_all(self.current())
        r = Renderer(self.current())
        if r.texts(new) != r.texts(self.temps):
            self.temps = new
            if not (self._movie and self._movie.state() == QMovie.MovieState.Running):
                self.update_preview()

    def update_preview(self):
        cfg = self.current()
        try:
            r = Renderer(cfg, 640)
            animated = r.animated
            self.drop.show_path(cfg["image"], animated)
            if animated:
                self._stop_movie()
                self._buffer = QBuffer()
                self._buffer.setData(QByteArray(r.gif(self.temps)))
                self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                self._movie = QMovie(self._buffer, QByteArray(b"gif"))
                self._movie.setScaledSize(QSize(PREVIEW, PREVIEW))
                self.preview.setMovie(self._movie)
                self._movie.start()
                self.preview_note.setText(f"Animated · {len(r._frames)} frames · live temps from this PC")
            else:
                self._stop_movie()
                pm = _pil_to_pixmap(r.still(self.temps)).scaled(
                    PREVIEW, PREVIEW, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.preview.setPixmap(pm)
                self.preview_note.setText("Live temps from this PC")
        except Exception as exc:
            self._stop_movie()
            self.preview.setText("Preview failed")
            self.preview_note.setText(str(exc))

    def _stop_movie(self):
        if self._movie is not None:
            self._movie.stop()
            self.preview.setMovie(None)
            self._movie = None

    def update_service_label(self):
        try:
            state = subprocess.run(["systemctl", "--user", "is-active", "kraken-lcd.service"],
                                   capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            state = "unknown"
        self.service_label.setWordWrap(state != "active")
        if state == "active":
            self.service_label.setText("●  Screen service running")
            self.service_label.setStyleSheet("color: #4ade80; font-size: 12px;")
        else:
            self.service_label.setText("Screen service isn't running, so saved changes won't reach the cooler.\n"
                                       "Start it:  systemctl --user enable --now kraken-lcd")
            self.service_label.setStyleSheet("color: #fbbf24; font-size: 12px;")

    def closeEvent(self, ev):
        if self.dirty():
            ans = QMessageBox.question(self, "Unsaved changes", "Save your changes before closing?",
                                       QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                       | QMessageBox.StandardButton.Cancel)
            if ans == QMessageBox.StandardButton.Cancel:
                ev.ignore()
                return
            if ans == QMessageBox.StandardButton.Save:
                self.save()
        ev.accept()


def main(argv=None):
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Kraken LCD")
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("kraken-lcd-settings")
    app.setStyle("Fusion")          # same look on every desktop; colours come from our stylesheet
    w = Window()
    w.resize(1120, 720)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
