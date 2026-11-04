"""CPU / GPU / liquid temperatures from the kernel's hwmon interface.

hwmon numbers change between boots, so sensors are found by driver name and
label every time.
"""

import glob
import shutil
import subprocess
import time
from pathlib import Path

# nzxt-kraken3 / nzxt-kraken2 hwmon names
KRAKEN_HWMON = ("kraken2024elite", "kraken2024plus", "kraken2023elite", "kraken2023",
                "z53", "x53", "kraken2")


def _hwmons():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            yield Path(d), Path(d, "name").read_text().strip()
        except OSError:
            continue


def _read(path):
    try:
        return int(Path(path).read_text()) / 1000
    except (OSError, ValueError):
        return None


def _by_label(hw, *labels):
    for lab in sorted(hw.glob("temp*_label")):
        try:
            text = lab.read_text().strip()
        except OSError:
            continue
        if text in labels:
            return _read(str(lab).replace("_label", "_input"))
    return None


def cpu_temp():
    for hw, name in _hwmons():
        if name == "coretemp":
            v = _by_label(hw, "Package id 0")
            if v is not None:
                return v
        elif name in ("k10temp", "zenpower"):
            v = _by_label(hw, "Tctl", "Tdie")
            if v is not None:
                return v
    for hw, name in _hwmons():
        if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            return _read(hw / "temp1_input")
    return None


_nv_cache = (0.0, None)


def _nvidia_smi():
    global _nv_cache
    now = time.monotonic()
    if now - _nv_cache[0] < 2.0:
        return _nv_cache[1]
    value = None
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3).stdout.split()
            value = float(out[0]) if out else None
        except (OSError, subprocess.SubprocessError, ValueError):
            value = None
    _nv_cache = (now, value)
    return value


def gpu_temp(sensor="edge"):
    for hw, name in _hwmons():
        if name == "amdgpu":
            v = _by_label(hw, sensor) or _by_label(hw, "edge")
            if v is not None:
                return v
    for hw, name in _hwmons():
        if name in ("nouveau", "radeon"):
            return _read(hw / "temp1_input")
    return _nvidia_smi()


def liquid_temp():
    for hw, name in _hwmons():
        if name in KRAKEN_HWMON or name.startswith("kraken"):
            v = _read(hw / "temp1_input")
            if v is not None:
                return v
    return None


def read_all(cfg, liquid_fallback=None):
    """Returns {"cpu": float|None, "gpu": ..., "liquid": ...}.

    liquid_fallback is called when the kernel driver isn't loaded (then the
    liquid temperature has to come from liquidctl itself)."""
    liquid = liquid_temp()
    if liquid is None and liquid_fallback is not None:
        try:
            liquid = liquid_fallback()
        except Exception:
            liquid = None
    return {"cpu": cpu_temp(), "gpu": gpu_temp(cfg.get("gpu_sensor", "edge")), "liquid": liquid}
