"""Drawing the screen: background (picture or GIF) + a minimal temperature overlay.

Layout is designed on a 640 px round panel and scaled for smaller ones.
The overlay is drawn at 2x and scaled down so rings and text stay smooth.
"""

import io
import logging
import math
import os
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageSequence

from . import config

LOG = logging.getLogger("kraken-lcd")
BASE = 640
SS = 2
NAMES = {"cpu": "CPU", "gpu": "GPU", "liquid": "LIQUID"}


# --------------------------------------------------------------------------- fonts

def _fc_match(pattern):
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


_FONT_CACHE = {}


def font_file(weight):
    """weight: 'light' for numbers, 'medium' for labels. Prefers Noto Sans."""
    if weight not in _FONT_CACHE:
        found = None
        for pattern in (f"Noto Sans:{weight}", f"DejaVu Sans:{weight}", "sans-serif"):
            path = _fc_match(pattern)
            if path and os.path.exists(path) and path.lower().endswith((".ttf", ".otf")):
                found = path
                break
        _FONT_CACHE[weight] = found
    return _FONT_CACHE[weight]


def _font(weight, px):
    path = font_file(weight)
    if path:
        return ImageFont.truetype(path, px)
    return ImageFont.load_default(size=px)


# --------------------------------------------------------------------------- colours

def rgb(c):
    c = c.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def is_animated(path):
    try:
        with Image.open(path) as im:
            return getattr(im, "is_animated", False) and im.format == "GIF"
    except Exception:
        return False


# --------------------------------------------------------------------------- renderer

class Renderer:
    """Turns temperatures into frames for a size x size round screen."""

    def __init__(self, cfg, size=BASE):
        self.cfg = cfg
        self.size = size
        self.k = size / BASE * SS               # base px -> overlay canvas px
        self.W = size * SS
        f = lambda w, px: _font(w, max(8, round(px * self.k)))
        self.f_big, self.f_deg = f("light", 184), f("light", 60)
        self.f_mid, self.f_mid_deg = f("light", 46), f("light", 24)
        self.f_label, self.f_small_label = f("medium", 20), f("medium", 15)
        self.accent = rgb(cfg["accent_color"])
        self.fg = rgb(cfg["text_color"])
        self.second = rgb(cfg.get("second_color") or cfg["text_color"])
        self._bg_key = None
        self._frames = None          # list of RGB frames (1 for a still picture)
        self._durations = None
        self._palettes = None

    # ---------------------------------------------------------------- background
    @property
    def animated(self):
        self._load_background()
        return len(self._frames) > 1

    def _load_background(self):
        path = config.image_path(self.cfg)
        try:
            mtime = os.path.getmtime(path) if path else None
        except OSError:
            mtime = None
        key = (path, mtime, self.cfg["dim"], self.cfg["style"], self.cfg["accent_color"],
               self.cfg["gif_max_frames"], self.size)
        if self._frames is not None and key == self._bg_key:
            return
        frames, durations = [], []
        if path:
            try:
                with Image.open(path) as im:
                    if getattr(im, "is_animated", False):
                        for fr in ImageSequence.Iterator(im):
                            frames.append(fr.convert("RGB"))
                            durations.append(max(20, int(fr.info.get("duration", 100) or 100)))
                    else:
                        frames.append(ImageOps.exif_transpose(im).convert("RGB"))
                        durations.append(0)
            except Exception as exc:
                LOG.warning("can't open %s (%s); using a plain background", path, exc)
                frames, durations = [], []
        if not frames:
            frames, durations = [Image.new("RGB", (self.size, self.size), "black")], [0]
        frames, durations = self._thin(frames, durations, int(self.cfg["gif_max_frames"]))
        self._frames = [self._process(fr) for fr in frames]
        self._durations = durations
        self._palettes = None
        self._bg_key = key

    @staticmethod
    def _thin(frames, durations, limit):
        if len(frames) <= limit or limit < 2:
            return frames, durations
        step = len(frames) / limit
        out_f, out_d = [], []
        for i in range(limit):
            a, b = int(i * step), int((i + 1) * step)
            out_f.append(frames[a])
            out_d.append(sum(durations[a:b]))
        return out_f, out_d

    def _process(self, img):
        S = self.size
        img = ImageOps.fit(img, (S, S), Image.Resampling.LANCZOS)
        if self.cfg["style"] == "duotone":
            g = ImageOps.autocontrast(img.convert("L"), cutoff=1)
            a = self.accent
            deep = tuple(round(v * 0.42) for v in a)
            light = tuple(round(v + (255 - v) * 0.55) for v in a)
            img = ImageOps.colorize(g, black=(0, 0, 0), mid=deep, white=light,
                                    blackpoint=35, midpoint=165, whitepoint=255)
        dim = max(0.0, min(1.0, float(self.cfg["dim"])))
        r = Image.radial_gradient("L").resize((S, S), Image.Resampling.BICUBIC)

        def shade(v):
            x = v / 255 * 1.41                       # 0 centre .. ~1 panel edge
            centre = 0.35 * max(0.0, 1 - x / 0.8) ** 1.6
            rim = 0.5 * max(0.0, (x - 0.8) / 0.2) ** 2
            return round(255 * min(0.95, dim + (1 - dim) * (centre + rim)))

        return Image.composite(Image.new("RGB", (S, S), "black"), img, r.point(shade))

    # ---------------------------------------------------------------- overlay
    def texts(self, temps):
        out = {}
        for k in config.SENSORS:
            v = temps.get(k)
            out[k] = "--" if v is None else (f"{v:.1f}" if k == "liquid" else f"{round(v)}")
        return out

    def _frac(self, key, v):
        lo, hi = self.cfg["ranges"].get(key, [20, 100])
        return 0.0 if v is None or hi == lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))

    @staticmethod
    def _spaced(d, xy, text, font, fill, spacing):
        widths = [d.textlength(ch, font=font) for ch in text]
        x = xy[0] - (sum(widths) + spacing * (len(text) - 1)) / 2
        for ch, w in zip(text, widths):
            d.text((x, xy[1]), ch, font=font, fill=fill, anchor="lm")
            x += w + spacing

    @staticmethod
    def _value(d, cx, cy, value, f_num, f_deg, fill):
        d.text((cx, cy), value, font=f_num, fill=fill, anchor="mm")
        if value != "--":
            l, t, r, b = d.textbbox((cx, cy), value, font=f_num, anchor="mm")
            gx0, gy0, _, _ = f_deg.getbbox("°", anchor="lt")
            d.text((r + f_deg.size * 0.06 - gx0, t - gy0), "°", font=f_deg, fill=fill, anchor="lt")

    def _arc(self, d, radius, width, a0, a1, fill):
        c = self.W / 2
        d.arc((c - radius, c - radius, c + radius, c + radius), a0, a1, fill=fill, width=round(width))
        rr = radius - width / 2
        for ang in (a0, a1):
            x, y = c + rr * math.cos(math.radians(ang)), c + rr * math.sin(math.radians(ang))
            d.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=fill)
        return c + rr * math.cos(math.radians(a1)), c + rr * math.sin(math.radians(a1))

    def ring_targets(self, temps):
        """Where each ring should be (0..1), keyed by sensor."""
        keys = self.cfg["show"][:2] if self.second_ring else self.cfg["show"][:1]
        return {key: self._frac(key, temps.get(key)) for key in keys}

    @property
    def second_ring(self):
        return bool(self.cfg.get("second_ring")) and len(self.cfg["show"]) > 1

    def _ring(self, layer, radius, width, frac, color, glow, tip):
        start, sweep = 135, 270
        self._arc(ImageDraw.Draw(layer), radius, width, start, start + sweep, (255, 255, 255, 26))
        if frac <= 0.005:
            return
        fill = Image.new("RGBA", (self.W, self.W), (0, 0, 0, 0))
        end = self._arc(ImageDraw.Draw(fill), radius, width, start, start + sweep * frac, color)
        if glow:
            layer.alpha_composite(fill.filter(ImageFilter.GaussianBlur(glow)))
        layer.alpha_composite(fill)
        if tip:
            r = width * 0.95
            ImageDraw.Draw(layer).ellipse((end[0] - r, end[1] - r, end[0] + r, end[1] + r), fill=(255, 255, 255, 255))

    def overlay(self, temps, rings=None):
        """Ring(s) + text as an RGBA image of the final size.

        rings: optional {sensor: 0..1} to draw the rings somewhere other than
        the current temperature (used for smooth movement)."""
        W, c, k = self.W, self.W / 2, self.k
        show = self.cfg["show"]
        vals = self.texts(temps)
        fg, soft, accent = self.fg + (255,), self.fg + (140,), self.accent + (255,)
        layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
        pos = self.ring_targets(temps)
        if rings:
            pos.update({key: v for key, v in rings.items() if key in pos})

        self._ring(layer, 296 * k, 7 * k, pos[show[0]], accent, 9 * k, tip=True)
        if self.second_ring:
            self._ring(layer, 281 * k, 4 * k, pos[show[1]], self.second + (230,), 5 * k, tip=False)

        txt = Image.new("RGBA", (W, W), (0, 0, 0, 0))
        d = ImageDraw.Draw(txt)
        self._spaced(d, (c, 200 * k), NAMES[show[0]], self.f_label, accent, 7 * k)
        self._value(d, c, 302 * k, vals[show[0]], self.f_big, self.f_deg, fg)
        rest = show[1:]
        if rest:
            xs = [c] if len(rest) == 1 else [c - 100 * k, c + 100 * k]
            if len(rest) == 2:
                d.line((c, 428 * k, c, 488 * k), fill=(255, 255, 255, 60), width=max(1, round(k)))
            for x, key in zip(xs, rest):
                self._value(d, x, 446 * k, vals[key], self.f_mid, self.f_mid_deg, fg)
                self._spaced(d, (x, 494 * k), NAMES[key], self.f_small_label, soft, 4 * k)
        shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
        shadow.putalpha(txt.getchannel("A").point(lambda v: v * 0.6).filter(ImageFilter.GaussianBlur(8 * k)))
        layer.alpha_composite(shadow, (0, round(2 * k)))
        layer.alpha_composite(txt)
        # scale down in premultiplied space so edges don't get dark fringes
        return layer.convert("RGBa").resize((self.size, self.size), Image.Resampling.LANCZOS).convert("RGBA")

    # ---------------------------------------------------------------- output
    def still(self, temps, frame=0, rings=None):
        """One RGB frame (the first GIF frame, or the picture)."""
        self._load_background()
        img = self._frames[frame % len(self._frames)].convert("RGBA")
        img.alpha_composite(self.overlay(temps, rings))
        return img.convert("RGB")

    def _palette_for(self, i):
        """Per-frame palette: 224 colours from the background + 32 for the overlay."""
        if self._palettes is None:
            self._palettes = [None] * len(self._frames)
        if self._palettes[i] is None:
            q = self._frames[i].quantize(224, method=Image.Quantize.MEDIANCUT)
            pal = q.getpalette()[: 224 * 3]
            pal += [0] * (224 * 3 - len(pal))
            extra = []
            for t in range(8):
                f = t / 7
                extra += [round(255 * f)] * 3                                         # black -> white
                extra += [round(a + (255 - a) * f) for a in self.accent]              # accent -> white
                extra += [round(a * (1 - f)) for a in self.accent]                    # accent -> black
                extra += [round(a * (1 - f)) for a in self.second]                    # 2nd ring -> black
            img = Image.new("P", (1, 1))
            img.putpalette(pal + extra)
            self._palettes[i] = img
        return self._palettes[i]

    @property
    def gif_length_ms(self):
        self._load_background()
        return sum(self._durations)

    def gif(self, temps, rings=None, start_ms=0):
        """Animated GIF bytes with the overlay on every frame.

        start_ms rotates the frame order so playback begins at that point of the
        animation; re-uploads then continue the loop instead of restarting it."""
        self._load_background()
        ov = self.overlay(temps, rings)
        n = len(self._frames)
        first, t = 0, 0
        total = sum(self._durations) or 1
        start_ms %= total
        for i, d in enumerate(self._durations):
            if t + d > start_ms:
                first = i
                break
            t += d
        order = list(range(first, n)) + list(range(0, first))
        out = []
        for i in order:
            img = self._frames[i].convert("RGBA")
            img.alpha_composite(ov)
            out.append(img.convert("RGB").quantize(palette=self._palette_for(i), dither=Image.Dither.NONE))
        buf = io.BytesIO()
        out[0].save(buf, format="GIF", save_all=True, append_images=out[1:],
                    duration=[self._durations[i] for i in order], loop=0, optimize=False, disposal=1)
        return buf.getvalue()
