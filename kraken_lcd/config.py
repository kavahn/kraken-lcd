"""Reading and writing ~/.config/kraken-lcd/config.toml.

The settings GUI only ever writes this file; the background service re-reads it
every few seconds, so changes show up on the screen without a restart.
"""

import os
import tempfile
import tomllib
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kraken-lcd"
CONFIG_PATH = CONFIG_DIR / "config.toml"

SENSORS = ("cpu", "gpu", "liquid")

DEFAULTS = {
    "image": "",                  # jpg / png / webp / animated gif; empty = plain black
    "style": "duotone",           # "duotone" (recoloured in the accent colour) or "original"
    "dim": 0.45,                  # 0 = picture untouched .. 1 = black
    "accent_color": "#ff5ca8",
    "text_color": "#ffffff",
    "show": ["cpu", "gpu", "liquid"],   # first = ring + big number, next two underneath
    "gpu_sensor": "edge",         # AMD: edge / junction / mem
    "brightness": None,           # 0-100, None = leave the cooler's setting alone
    "orientation": None,          # 0 / 90 / 180 / 270, None = leave the cooler's setting alone
    "interval": 2.0,              # seconds between temperature checks
    "keepalive": 30.0,            # re-send a still frame at least this often
    "gif_refresh": 15.0,          # seconds between GIF re-uploads with new temps
    "gif_max_frames": 48,         # longer GIFs are thinned out to keep uploads quick
    "ranges": {"cpu": [25, 95], "gpu": [25, 95], "liquid": [20, 45]},
}

HEADER = """\
# kraken-lcd settings. Edit here or with the "Kraken LCD Settings" app.
# The kraken-lcd service picks up changes within a few seconds.
"""

COMMENTS = {
    "image": "Background picture: jpg, png, webp or an animated gif. Empty = plain black.",
    "style": '"duotone" recolours the picture in black / accent / white, "original" shows it as-is.',
    "dim": "Darken the picture so the numbers stand out: 0 = untouched, 1 = black.",
    "accent_color": "Ring, main label and duotone colour.",
    "text_color": "Colour of the numbers.",
    "show": 'Which temps to show: first gets the ring and the big number. Options: "cpu", "gpu", "liquid".',
    "gpu_sensor": 'AMD GPU sensor: "edge" (what most apps show), "junction" (hotspot) or "mem".',
    "brightness": "Screen brightness 0-100. Remove the line to keep the cooler's own setting.",
    "orientation": "Screen rotation: 0, 90, 180 or 270. Remove the line to keep the cooler's own setting.",
    "interval": "How often to check temps (seconds). Still pictures only re-upload when a number changes.",
    "keepalive": "Re-send a still picture at least this often, in case the cooler reset its screen.",
    "gif_refresh": "Animated GIFs are re-uploaded with fresh temps at most this often (seconds).",
    "gif_max_frames": "GIFs with more frames than this are thinned out so uploads stay quick.",
    "ranges": "Temperature at which the ring is empty / full.",
}


def load(path=CONFIG_PATH):
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        ranges = user.pop("ranges", {})
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
        cfg["ranges"].update({k: list(v) for k, v in ranges.items() if k in SENSORS})
    cfg["show"] = [s for s in cfg["show"] if s in SENSORS][:3] or ["cpu"]
    return cfg


def image_path(cfg):
    p = (cfg.get("image") or "").strip()
    return os.path.expanduser(p) if p else ""


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"can't write {v!r} to TOML")


def save(cfg, path=CONFIG_PATH):
    """Write the config atomically (the service may read it at any moment)."""
    lines = [HEADER]
    for key in DEFAULTS:
        if key == "ranges":
            continue
        value = cfg.get(key, DEFAULTS[key])
        lines.append(f"# {COMMENTS[key]}")
        if value is None:
            lines.append(f"# {key} = {_toml_value(50 if key == 'brightness' else 0)}")
        else:
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    lines.append(f"# {COMMENTS['ranges']}")
    lines.append("[ranges]")
    for s in SENSORS:
        lines.append(f"{s} = {_toml_value(cfg['ranges'].get(s, DEFAULTS['ranges'][s]))}")
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)  # never write something we can't read back
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
