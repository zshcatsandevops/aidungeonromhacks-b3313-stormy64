#!/usr/bin/env python3
"""
snesemu4k — AC SNES player (libretro host, mewsnes-class ctypes).

FILES=OFF: no embedded core or ROM; pick or download at runtime.
Picking a ROM with no usable local SNES core queues a snes9x libretro
download from buildbot, then loads the ROM when the core is ready.

Bug-hardening vs typical pygame + ctypes hosts
-----------------------------------------------
  * NumPy never keeps a live view into libretro video RAM — buffers are
    copied before ``frombuffer`` / reshape.
  * ``pygame.image.frombuffer`` surfaces are ``.copy()`` so the next core
    frame cannot corrupt pixels mid-blit.
  * ``retro_load_game`` failure clears the ROM ctypes buffer reference.
  * Windows: ``os.add_dll_directory`` for core sibling DLLs.
  * Mixer: bounded audio queue (no unbounded latency).

Optional: ``mewsnes_fast`` RGB565 converter (``setup_mewsnes.py``).

Requirements: ``pip install pygame numpy``
"""

from __future__ import annotations

import ctypes
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
import zipfile
from ctypes import (
    CFUNCTYPE, POINTER, Structure, byref,
    c_bool, c_char_p, c_double, c_float, c_int16, c_size_t,
    c_uint, c_uint8, c_uint16, c_void_p,
)
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import error as _urllib_error
from urllib import request as _urllib_request

try:
    import pygame
except ImportError:
    print("pip install pygame", file=sys.stderr)
    sys.exit(1)

try:
    import numpy as np
    _HAS_NP = True
except ImportError:
    _HAS_NP = False

# Tk + pygame-ce/SDL on macOS: Tk calls -[NSApplication macOSVersion] but SDL
# replaces NSApplication with SDLApplication → crash. Skip tk entirely on Darwin.
_HAS_TK = False
tk = None  # type: ignore[assignment]
filedialog = None  # type: ignore[assignment]
if platform.system() != "Darwin":
    try:
        import tkinter as tk
        from tkinter import filedialog

        _HAS_TK = True
    except ImportError:
        pass

try:
    from mewsnes_fast import convert_rgb565 as _fast_565  # type: ignore

    _HAS_FAST = True
except ImportError:
    _HAS_FAST = False

APP_ID = "snesemu4k"
PYTHON_TARGET = "3.14"
FILES_OFF = True
TARGET_FPS = 60

if sys.version_info < (3, 10):
    print(f"{APP_ID} expects Python 3.10+; Python {PYTHON_TARGET} is supported.", file=sys.stderr)
    sys.exit(1)

TEXT = (210, 220, 240)
TEXT_DIM = (130, 140, 155)
BTN_BG = (24, 26, 32)
BTN_HI = (42, 48, 62)
EDGE = (90, 100, 120)


def _can_pick_files() -> bool:
    if platform.system() == "Darwin":
        return shutil.which("osascript") is not None
    return bool(_HAS_TK)


def _pick_file_path(
    title: str,
    filetypes: list[tuple[str, str]],
    tk_root: Optional[Any],
) -> Optional[str]:
    """File dialog that does not use Tk on macOS (avoids SDLApplication conflict)."""
    if platform.system() == "Darwin":
        esc = title.replace("\\", "\\\\").replace('"', '\\"')
        script = f'POSIX path of (choose file with prompt "{esc}")'
        try:
            out = subprocess.check_output(
                ["osascript", "-e", script],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            p = out.strip()
            return p or None
        except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
    if _HAS_TK and tk_root is not None and filedialog is not None:
        try:
            p = filedialog.askopenfilename(
                parent=tk_root, title=title, filetypes=filetypes
            )
            return str(p) if p else None
        except Exception:
            return None
    return None


def _void_bg(surf: pygame.Surface, t: float) -> None:
    w, h = surf.get_size()
    for yy in range(0, h, 10):
        for xx in range(0, w, 10):
            u = (math.sin(t * 0.002 + xx * 0.02) + 1) * 0.5
            v = (math.cos(t * 0.0017 + yy * 0.018) + 1) * 0.5
            c = int(8 + u * 18), int(10 + v * 22), int(18 + (u + v) * 28)
            surf.fill(c, (xx, yy, 10, 10))


# --- libretro ---
class retro_game_info(Structure):
    _fields_ = [
        ("path", c_char_p),
        ("data", c_void_p),
        ("size", c_size_t),
        ("meta", c_char_p),
    ]


class retro_system_info(Structure):
    _fields_ = [
        ("library_name", c_char_p),
        ("library_version", c_char_p),
        ("valid_extensions", c_char_p),
        ("need_fullpath", c_bool),
        ("block_extract", c_bool),
    ]


class retro_game_geometry(Structure):
    _fields_ = [
        ("base_width", c_uint),
        ("base_height", c_uint),
        ("max_width", c_uint),
        ("max_height", c_uint),
        ("aspect_ratio", c_float),
    ]


class retro_system_timing(Structure):
    _fields_ = [
        ("fps", c_double),
        ("sample_rate", c_double),
    ]


class retro_system_av_info(Structure):
    _fields_ = [
        ("geometry", retro_game_geometry),
        ("timing", retro_system_timing),
    ]


VIDEO_CB = CFUNCTYPE(None, c_void_p, c_uint, c_uint, c_size_t)
AUDIO_SAMPLE_CB = CFUNCTYPE(None, c_int16, c_int16)
AUDIO_BATCH_CB = CFUNCTYPE(c_size_t, c_void_p, c_size_t)
INPUT_POLL_CB = CFUNCTYPE(None)
INPUT_STATE_CB = CFUNCTYPE(c_int16, c_uint, c_uint, c_uint, c_uint)
ENV_CB = CFUNCTYPE(c_bool, c_uint, c_void_p)

RETRO_API_VERSION = 1
RETRO_DEVICE_JOYPAD = 1
RETRO_DEVICE_ID_JOYPAD_B = 0
RETRO_DEVICE_ID_JOYPAD_Y = 1
RETRO_DEVICE_ID_JOYPAD_SELECT = 2
RETRO_DEVICE_ID_JOYPAD_START = 3
RETRO_DEVICE_ID_JOYPAD_UP = 4
RETRO_DEVICE_ID_JOYPAD_DOWN = 5
RETRO_DEVICE_ID_JOYPAD_LEFT = 6
RETRO_DEVICE_ID_JOYPAD_RIGHT = 7
RETRO_DEVICE_ID_JOYPAD_A = 8
RETRO_DEVICE_ID_JOYPAD_X = 9
RETRO_DEVICE_ID_JOYPAD_L = 10
RETRO_DEVICE_ID_JOYPAD_R = 11

RETRO_ENVIRONMENT_GET_OVERSCAN = 2
RETRO_ENVIRONMENT_GET_CAN_DUPE = 3
RETRO_ENVIRONMENT_SET_PERFORMANCE_LEVEL = 8
RETRO_ENVIRONMENT_SET_PIXEL_FORMAT = 10
RETRO_ENVIRONMENT_SET_VARIABLES = 16
RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE = 17
RETRO_ENVIRONMENT_GET_LANGUAGE = 39

RETRO_PIXEL_FORMAT_0RGB1555 = 0
RETRO_PIXEL_FORMAT_XRGB8888 = 1
RETRO_PIXEL_FORMAT_RGB565 = 2


class LibretroHost:
    """libretro ctypes host (SNES / any core)."""

    def __init__(self) -> None:
        self.dll: Optional[ctypes.CDLL] = None
        self.library_name = ""
        self.library_version = ""
        self.pixel_format = RETRO_PIXEL_FORMAT_0RGB1555
        self.base_width = 256
        self.base_height = 224
        self.fps = 60.0
        self.sample_rate = 32040.0
        self.frame_w = self.frame_h = 0
        self.frame_rgb888: Optional[bytes] = None
        self.audio_buffer = bytearray()
        self.inputs: dict[tuple[int, int], bool] = {}
        self._cb_env = ENV_CB(self._env_cb)
        self._cb_video = VIDEO_CB(self._video_cb)
        self._cb_audio_sample = AUDIO_SAMPLE_CB(self._audio_sample_cb)
        self._cb_audio_batch = AUDIO_BATCH_CB(self._audio_batch_cb)
        self._cb_input_poll = INPUT_POLL_CB(self._input_poll_cb)
        self._cb_input_state = INPUT_STATE_CB(self._input_state_cb)
        self._rom_buf = None
        self._dll_dir_handle = None
        self.loaded = False
        self.rom_loaded = False
        self.log: list[str] = []

    def load_core(self, path: str) -> str:
        self.unload()
        core_dir = str(Path(path).parent)
        if sys.platform.startswith("win"):
            try:
                self._dll_dir_handle = os.add_dll_directory(core_dir)
            except (OSError, AttributeError):
                self._dll_dir_handle = None
        try:
            dll = ctypes.CDLL(str(path))
        except OSError as e:
            return f"load_core: {e}"
        try:
            self._bind(dll)
        except AttributeError as e:
            return f"missing export: {e}"
        if dll.retro_api_version() != RETRO_API_VERSION:
            return "libretro API mismatch"
        si = retro_system_info()
        dll.retro_get_system_info(byref(si))
        self.library_name = (si.library_name or b"?").decode("latin-1", "replace")
        self.library_version = (si.library_version or b"?").decode("latin-1", "replace")
        dll.retro_set_environment(self._cb_env)
        dll.retro_set_video_refresh(self._cb_video)
        dll.retro_set_audio_sample(self._cb_audio_sample)
        dll.retro_set_audio_sample_batch(self._cb_audio_batch)
        dll.retro_set_input_poll(self._cb_input_poll)
        dll.retro_set_input_state(self._cb_input_state)
        dll.retro_init()
        self.dll = dll
        self.loaded = True
        self._log(f"[core] {self.library_name} {self.library_version}")
        return ""

    def load_rom(self, data: bytes) -> str:
        if not self.loaded or self.dll is None:
            return "core not loaded"
        if not data:
            return "empty ROM"
        buf = (c_uint8 * len(data)).from_buffer_copy(data)
        info = retro_game_info()
        info.path = None
        info.data = ctypes.cast(buf, c_void_p)
        info.size = len(data)
        info.meta = None
        ok = self.dll.retro_load_game(byref(info))
        if not ok:
            self._rom_buf = None
            return "retro_load_game failed"
        self._rom_buf = buf
        av = retro_system_av_info()
        self.dll.retro_get_system_av_info(byref(av))
        self.base_width = int(av.geometry.base_width or 256)
        self.base_height = int(av.geometry.base_height or 224)
        self.fps = float(TARGET_FPS)  # fixed 60 FPS pacing target
        self.sample_rate = float(av.timing.sample_rate or 32040.0)
        self.rom_loaded = True
        self._log(f"[rom] {len(data)} B  {self.base_width}x{self.base_height}")
        return ""

    def run_frame(self) -> None:
        if self.loaded and self.rom_loaded and self.dll:
            self.audio_buffer = bytearray()
            self.dll.retro_run()

    def reset(self) -> None:
        if self.loaded and self.rom_loaded and self.dll:
            self.dll.retro_reset()
            self._log("[reset]")

    def unload(self) -> None:
        if self.dll is not None:
            try:
                if self.rom_loaded:
                    self.dll.retro_unload_game()
                self.dll.retro_deinit()
            except Exception:
                pass
        self.dll = None
        self.loaded = False
        self.rom_loaded = False
        self.frame_rgb888 = None
        self._rom_buf = None
        if self._dll_dir_handle is not None:
            try:
                self._dll_dir_handle.close()
            except Exception:
                pass
            self._dll_dir_handle = None

    def set_button(self, port: int, bid: int, on: bool) -> None:
        self.inputs[(port, bid)] = bool(on)

    @staticmethod
    def _bind(dll: ctypes.CDLL) -> None:
        for name, rest, args in [
            ("retro_api_version", c_uint, []),
            ("retro_init", None, []),
            ("retro_deinit", None, []),
            ("retro_get_system_info", None, [POINTER(retro_system_info)]),
            ("retro_get_system_av_info", None, [POINTER(retro_system_av_info)]),
            ("retro_set_environment", None, [ENV_CB]),
            ("retro_set_video_refresh", None, [VIDEO_CB]),
            ("retro_set_audio_sample", None, [AUDIO_SAMPLE_CB]),
            ("retro_set_audio_sample_batch", None, [AUDIO_BATCH_CB]),
            ("retro_set_input_poll", None, [INPUT_POLL_CB]),
            ("retro_set_input_state", None, [INPUT_STATE_CB]),
            ("retro_load_game", c_bool, [POINTER(retro_game_info)]),
            ("retro_unload_game", None, []),
            ("retro_run", None, []),
            ("retro_reset", None, []),
        ]:
            f = getattr(dll, name)
            f.restype = rest
            f.argtypes = args

    def _env_cb(self, cmd: int, data: int) -> bool:
        if cmd == RETRO_ENVIRONMENT_GET_OVERSCAN:
            if data:
                ctypes.cast(data, POINTER(c_bool))[0] = False
            return True
        if cmd == RETRO_ENVIRONMENT_GET_CAN_DUPE:
            if data:
                ctypes.cast(data, POINTER(c_bool))[0] = True
            return True
        if cmd == RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
            if data:
                fmt = ctypes.cast(data, POINTER(c_uint))[0]
                if int(fmt) in (0, 1, 2):
                    self.pixel_format = int(fmt)
                    self._log(f"[env] pixel_format={fmt}")
                    return True
            return False
        if cmd in (RETRO_ENVIRONMENT_SET_PERFORMANCE_LEVEL, RETRO_ENVIRONMENT_SET_VARIABLES):
            return True
        if cmd == RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE:
            if data:
                ctypes.cast(data, POINTER(c_bool))[0] = False
            return True
        if cmd == RETRO_ENVIRONMENT_GET_LANGUAGE:
            if data:
                ctypes.cast(data, POINTER(c_uint))[0] = 0
            return True
        return False

    def _video_cb(self, data: int, width: int, height: int, pitch: int) -> None:
        if not data or not width or not height:
            return
        w, h, p = int(width), int(height), int(pitch)
        self.frame_w, self.frame_h = w, h
        try:
            if self.pixel_format == RETRO_PIXEL_FORMAT_RGB565:
                raw = self._conv565(data, w, h, p)
            elif self.pixel_format == RETRO_PIXEL_FORMAT_0RGB1555:
                raw = self._conv1555(data, w, h, p)
            else:
                raw = self._conv8888(data, w, h, p)
            self.frame_rgb888 = bytes(raw)
        except Exception as e:  # noqa: BLE001
            self._log(f"[video] {e}")

    def _audio_sample_cb(self, lo: int, hi: int) -> None:
        self.audio_buffer += int(lo).to_bytes(2, "little", signed=True)
        self.audio_buffer += int(hi).to_bytes(2, "little", signed=True)

    def _audio_batch_cb(self, data: int, frames: int) -> int:
        n = int(frames)
        if not data or n <= 0:
            return n
        buf = (c_uint8 * (n * 4)).from_address(data)
        self.audio_buffer += bytes(buf)
        return n

    def _input_poll_cb(self) -> None:
        return

    def _input_state_cb(self, port: int, device: int, _i: int, id_: int) -> int:
        if device != RETRO_DEVICE_JOYPAD:
            return 0
        return 1 if self.inputs.get((int(port), int(id_)), False) else 0

    def _np_copy_frame(self, addr: int, pitch: int, h: int) -> bytes:
        """Copy VRAM into a standalone Python buffer (no dangling ctypes view)."""
        nbytes = pitch * h
        buf = (c_uint8 * nbytes).from_address(addr)
        return bytes(buf)

    def _conv565(self, addr: int, w: int, h: int, pitch: int) -> bytes:
        if _HAS_FAST:
            return bytes(_fast_565(addr, w, h, pitch))
        blob = self._np_copy_frame(addr, pitch, h)
        if not _HAS_NP:
            return self._slow565(blob, w, h, pitch, False)
        full = np.frombuffer(blob, dtype=np.uint8).reshape(h, pitch).copy()
        row16 = full[:, : w * 2].copy().view(np.uint16).reshape(h, w)
        r = ((row16 >> 11) & 0x1F).astype(np.uint8) << 3
        g = ((row16 >> 5) & 0x3F).astype(np.uint8) << 2
        b = (row16 & 0x1F).astype(np.uint8) << 3
        return np.ascontiguousarray(np.dstack([r, g, b])).tobytes()

    def _conv1555(self, addr: int, w: int, h: int, pitch: int) -> bytes:
        blob = self._np_copy_frame(addr, pitch, h)
        if not _HAS_NP:
            return self._slow565(blob, w, h, pitch, True)
        full = np.frombuffer(blob, dtype=np.uint8).reshape(h, pitch).copy()
        row16 = full[:, : w * 2].copy().view(np.uint16).reshape(h, w)
        r = ((row16 >> 10) & 0x1F).astype(np.uint8) << 3
        g = ((row16 >> 5) & 0x1F).astype(np.uint8) << 3
        b = (row16 & 0x1F).astype(np.uint8) << 3
        return np.ascontiguousarray(np.dstack([r, g, b])).tobytes()

    def _conv8888(self, addr: int, w: int, h: int, pitch: int) -> bytes:
        blob = self._np_copy_frame(addr, pitch, h)
        if _HAS_NP:
            full = np.frombuffer(blob, dtype=np.uint8).reshape(h, pitch).copy()
            rgbx = full[:, : w * 4].reshape(h, w, 4)
            return np.ascontiguousarray(rgbx[:, :, [2, 1, 0]]).tobytes()
        out = bytearray(w * h * 3)
        for y in range(h):
            for x in range(w):
                so = y * pitch + x * 4
                do = (y * w + x) * 3
                out[do] = blob[so + 2]
                out[do + 1] = blob[so + 1]
                out[do + 2] = blob[so + 0]
        return bytes(out)

    def _slow565(self, src: bytes, w: int, h: int, pitch: int, f15: bool) -> bytes:
        out = bytearray(w * h * 3)
        for y in range(h):
            for x in range(w):
                so = y * pitch + x * 2
                px = src[so] | (src[so + 1] << 8)
                if f15:
                    r, g, b = ((px >> 10) & 0x1F) << 3, ((px >> 5) & 0x1F) << 3, (px & 0x1F) << 3
                else:
                    r, g, b = ((px >> 11) & 0x1F) << 3, ((px >> 5) & 0x3F) << 2, (px & 0x1F) << 3
                do = (y * w + x) * 3
                out[do : do + 3] = bytes((r, g, b))
        return bytes(out)

    def _log(self, s: str) -> None:
        self.log.append(s)
        self.log = self.log[-48:]


# --- cores ---
LIBRETRO_BUILDBOT_BASE = "https://buildbot.libretro.com/nightly"
_DEFAULT_SNES_CORE = "snes9x"
_SNES_PREFS = ("snes9x", "snes9x2010", "snes9x2005", "bsnes_mercury_balanced", "bsnes")


def _retro_dirs() -> list[Path]:
    out: list[Path] = []
    if sys.platform.startswith("win"):
        for ev in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            b = os.environ.get(ev)
            if not b:
                continue
            for sub in (r"RetroArch-Win64\cores", r"RetroArch\cores", r"RetroArch-Win32\cores"):
                out.append(Path(b) / sub)
        out += [
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\RetroArch\cores"),
            Path(r"C:\Program Files\Steam\steamapps\common\RetroArch\cores"),
        ]
    elif sys.platform == "darwin":
        h = Path.home()
        out += [h / "Library/Application Support/RetroArch/cores", Path("/Applications/RetroArch.app/Contents/Resources/cores")]
    else:
        h = Path.home()
        out += [h / ".config/retroarch/cores", Path("/usr/lib/libretro")]
    try:
        out.append(Path(__file__).resolve().parent / "cores")
    except NameError:
        out.append(Path.cwd() / "cores")
    return out


def _ext() -> str:
    if sys.platform.startswith("win"):
        return "dll"
    if sys.platform == "darwin":
        return "dylib"
    return "so"


def discover_snes_cores() -> list[Path]:
    e = _ext()
    found: dict[str, Path] = {}
    for d in _retro_dirs():
        try:
            if not d.is_dir():
                continue
            for f in d.glob(f"*_libretro.{e}"):
                n = f.name.lower()
                if not any(t in n for t in ("snes", "bsnes")):
                    continue
                found.setdefault(f.name, f.resolve())
        except OSError:
            pass

    def rank(p: Path) -> tuple[int, str]:
        n = p.name.lower()
        for i, pref in enumerate(_SNES_PREFS):
            if n.startswith(pref + "_libretro"):
                return (i, n)
        return (99, n)

    return sorted(found.values(), key=rank)


def _plat() -> tuple[str, str]:
    m = (platform.machine() or "").lower()
    big = sys.maxsize > 2**32
    if sys.platform.startswith("win"):
        if "arm" in m and big:
            return "windows/arm64", "dll"
        return ("windows/x86_64", "dll") if big or m in ("amd64", "x86_64") else ("windows/x86", "dll")
    if sys.platform == "darwin":
        return ("apple/osx/arm64", "dylib") if "arm" in m or m == "arm64" else ("apple/osx/x86_64", "dylib")
    if "aarch64" in m or "arm64" in m:
        return "linux/armv8", "so"
    if "arm" in m:
        return "linux/armhf", "so"
    return ("linux/x86_64", "so") if big else ("linux/i686", "so")


def download_snes_core(
    name: str = _DEFAULT_SNES_CORE,
    dest: Optional[Path] = None,
    timeout: float = 90.0,
    prog: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[Optional[Path], str]:
    try:
        plat, x = _plat()
    except Exception as e:
        return None, str(e)
    dest = dest or (Path(__file__).resolve().parent / "cores")
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, str(e)
    fn = f"{name}_libretro.{x}"
    url = f"{LIBRETRO_BUILDBOT_BASE}/{plat}/latest/{fn}.zip"
    if prog:
        prog(0, 0, url)
    tmp: Optional[Path] = None
    try:
        req = _urllib_request.Request(url, headers={"User-Agent": f"{APP_ID}/1.0"})
        with _urllib_request.urlopen(req, timeout=timeout) as resp:
            tot = int(resp.headers.get("Content-Length", "0") or 0)
            done = 0
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as t:
                tmp = Path(t.name)
                while True:
                    b = resp.read(65536)
                    if not b:
                        break
                    t.write(b)
                    done += len(b)
                    if prog:
                        prog(done, tot or done, fn)
        with zipfile.ZipFile(tmp, "r") as zf:
            zf.extractall(dest)
        p = dest / fn
        if not p.exists():
            for g in dest.glob(f"{name}_libretro*.{x}"):
                p = g
                break
        if not p.exists():
            return None, "core not in zip"
        return p, ""
    except _urllib_error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except _urllib_error.URLError as e:
        return None, str(e.reason)
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    finally:
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def snes_cart_title(rom: bytes) -> str:
    if len(rom) >= 512 and len(rom) % 1024 == 512:
        rom = rom[512:]
    if len(rom) < 0xFFB0:
        return "?"
    t1 = rom[0x7FC0 : 0x7FC0 + 21]
    t2 = rom[0xFFC0 : 0xFFC0 + 21]
    s1 = sum(1 for b in t1 if 32 <= b < 127)
    s2 = sum(1 for b in t2 if 32 <= b < 127)
    raw = t2 if s2 >= s1 else t1
    return raw.decode("latin-1", errors="replace").strip("\x00 ").strip() or "?"


KEYMAP = {
    pygame.K_UP: RETRO_DEVICE_ID_JOYPAD_UP,
    pygame.K_DOWN: RETRO_DEVICE_ID_JOYPAD_DOWN,
    pygame.K_LEFT: RETRO_DEVICE_ID_JOYPAD_LEFT,
    pygame.K_RIGHT: RETRO_DEVICE_ID_JOYPAD_RIGHT,
    pygame.K_z: RETRO_DEVICE_ID_JOYPAD_B,
    pygame.K_x: RETRO_DEVICE_ID_JOYPAD_A,
    pygame.K_a: RETRO_DEVICE_ID_JOYPAD_Y,
    pygame.K_s: RETRO_DEVICE_ID_JOYPAD_X,
    pygame.K_q: RETRO_DEVICE_ID_JOYPAD_L,
    pygame.K_w: RETRO_DEVICE_ID_JOYPAD_R,
    pygame.K_RETURN: RETRO_DEVICE_ID_JOYPAD_START,
    pygame.K_BACKSPACE: RETRO_DEVICE_ID_JOYPAD_SELECT,
}
_PAD = tuple(KEYMAP.values())


def btn(surf, font, lab, rect, m, on=True):
    h = on and rect.collidepoint(m)
    pygame.draw.rect(surf, BTN_HI if h else BTN_BG, rect, border_radius=6)
    pygame.draw.rect(surf, EDGE if on else (60, 60, 70), rect, 1, border_radius=6)
    c = TEXT if on else TEXT_DIM
    t = font.render(lab, True, c)
    surf.blit(t, (rect.centerx - t.get_width() // 2, rect.centery - t.get_height() // 2))


def main() -> None:
    pygame.mixer.pre_init(frequency=48000, size=-16, channels=2, buffer=1024)
    pygame.init()
    try:
        pygame.mixer.init()
        mix = True
    except pygame.error:
        mix = False
    ch = pygame.mixer.Channel(0) if mix else None

    pygame.display.set_caption(f"{APP_ID} — Python {PYTHON_TARGET} — libretro SNES — FILES=OFF")
    scr = pygame.display.set_mode((1000, 640))
    clk = pygame.time.Clock()
    try:
        ft = pygame.font.SysFont("consolas", 18, bold=True)
        fb = pygame.font.SysFont("consolas", 13)
        fs = pygame.font.SysFont("consolas", 11)
    except Exception:
        ft = fb = fs = pygame.font.Font(None, 16)

    host = LibretroHost()
    kb: set[int] = set()
    cart = ""
    root = None
    if _HAS_TK and tk is not None:
        root = tk.Tk()
        root.withdraw()

    dl: dict[str, object] = {"busy": False, "new": False, "path": None, "err": ""}
    lk = threading.Lock()
    pending_rom: Optional[bytes] = None

    def dl_job():
        def p(a, b, m):
            pass

        path, err = download_snes_core(_DEFAULT_SNES_CORE, prog=p)
        with lk:
            dl["busy"] = False
            dl["new"] = True
            dl["path"] = str(path) if path else None
            dl["err"] = err

    mrg, hh = 14, 40
    view = pygame.Rect(mrg, hh + mrg, 768, 504)
    side = pygame.Rect(view.right + mrg, view.y, scr.get_width() - view.right - 2 * mrg, view.height)
    yb = scr.get_height() - 48
    b1 = pygame.Rect(mrg, yb, 110, 38)
    b2 = pygame.Rect(mrg + 118, yb, 118, 38)
    b3 = pygame.Rect(mrg + 244, yb, 100, 38)
    b4 = pygame.Rect(mrg + 352, yb, 90, 38)
    b5 = pygame.Rect(mrg + 450, yb, 80, 38)
    b6 = pygame.Rect(mrg + 538, yb, 90, 38)
    b7 = pygame.Rect(mrg + 636, yb, 80, 38)

    toast, tn = "", 0

    def toast_msg(s: str, n: int = 100):
        nonlocal toast, tn
        toast, tn = s, n

    def load_core(p: str) -> None:
        nonlocal cart
        err = host.load_core(p)
        if err:
            toast_msg(err, 200)
        else:
            kb.clear()
            cart = ""
            pygame.display.set_caption(f"{APP_ID} — {host.library_name}")
            toast_msg("core ok", 80)

    def apply_rom_bytes(data: bytes) -> None:
        nonlocal cart, pause
        err = host.load_rom(data)
        if err:
            toast_msg(err, 200)
        else:
            cart = snes_cart_title(data)
            pause = False
            toast_msg(cart[:36], 120)

    def ensure_core_then_rom(data: bytes) -> None:
        """Load ROM now if a core is ready; else use local core or auto-download snes9x."""
        nonlocal pending_rom
        if host.loaded:
            apply_rom_bytes(data)
            return
        cores = discover_snes_cores()
        if cores:
            load_core(str(cores[0]))
            if host.loaded:
                apply_rom_bytes(data)
                return
        pending_rom = data
        start_dl = False
        with lk:
            if dl["busy"]:
                toast_msg("Core download in progress — ROM will load when ready", 160)
            else:
                dl["busy"] = True
                start_dl = True
        if start_dl:
            threading.Thread(target=dl_job, daemon=True).start()
            toast_msg("No SNES core found — downloading snes9x…", 200)

    go = True
    pause = False
    mute = False
    while go:
        t = pygame.time.get_ticks()
        mx, my = pygame.mouse.get_pos()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                go = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                if b1.collidepoint(ev.pos):
                    if not _can_pick_files():
                        toast_msg("file picker unavailable", 80)
                    else:
                        cores = discover_snes_cores()
                        if cores:
                            host.unload()
                            load_core(str(cores[0]))
                        else:
                            p = _pick_file_path(
                                "libretro SNES core",
                                [("libretro", f"*.{_ext()}"), ("All", "*.*")],
                                root,
                            )
                            if p:
                                load_core(p)
                elif b2.collidepoint(ev.pos):
                    start_dl = False
                    with lk:
                        if dl["busy"]:
                            toast_msg("download active", 60)
                        else:
                            dl["busy"] = True
                            start_dl = True
                    if start_dl:
                        threading.Thread(target=dl_job, daemon=True).start()
                        toast_msg("downloading snes9x…", 160)
                elif b3.collidepoint(ev.pos):
                    if not _can_pick_files():
                        toast_msg("file picker unavailable", 80)
                    else:
                        p = _pick_file_path(
                            "SNES ROM",
                            [
                                ("SNES", "*.sfc *.smc *.SFC *.SMC"),
                                ("All", "*.*"),
                            ],
                            root,
                        )
                        if p:
                            ensure_core_then_rom(Path(p).read_bytes())
                elif b4.collidepoint(ev.pos) and host.rom_loaded:
                    pause = not pause
                elif b5.collidepoint(ev.pos) and host.rom_loaded:
                    host.reset()
                elif b6.collidepoint(ev.pos):
                    mute = not mute
                elif b7.collidepoint(ev.pos):
                    host.unload()
                    kb.clear()
                    cart = ""
                    pygame.display.set_caption(f"{APP_ID} — Python {PYTHON_TARGET} — libretro SNES — FILES=OFF")
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_F1 and host.rom_loaded:
                    host.reset()
                elif ev.key == pygame.K_F2:
                    mute = not mute
                elif ev.key in KEYMAP and host.loaded:
                    kb.add(KEYMAP[ev.key])
            elif ev.type == pygame.KEYUP:
                if ev.key in KEYMAP:
                    kb.discard(KEYMAP[ev.key])

        cpath: Optional[str] = None
        cerr: object = None
        with lk:
            if dl["new"]:
                dl["new"] = False
                p_raw = dl["path"]
                cpath = str(p_raw) if p_raw else None
                cerr = dl["err"]
        if cpath:
            load_core(str(cpath))
            if host.loaded and pending_rom is not None:
                rom_b = pending_rom
                pending_rom = None
                apply_rom_bytes(rom_b)
            elif pending_rom is not None and not host.loaded:
                pending_rom = None
                toast_msg("Core failed after download", 220)
        elif cerr:
            toast_msg(str(cerr), 220)
            pending_rom = None

        _void_bg(scr, float(t))
        pygame.draw.rect(scr, BTN_BG, (0, 0, scr.get_width(), hh))
        pygame.draw.line(scr, EDGE, (0, hh - 1), (scr.get_width(), hh - 1))
        scr.blit(ft.render(f"{APP_ID}  ·  Python {PYTHON_TARGET}  ·  60 FPS  ·  FILES_OFF={FILES_OFF}", True, TEXT), (mrg, 10))

        pygame.draw.rect(scr, (0, 0, 0), view, border_radius=4)
        pygame.draw.rect(scr, EDGE, view, 2, border_radius=4)
        inner = view.inflate(-18, -18)

        if host.loaded:
            for bid in _PAD:
                host.set_button(0, bid, bid in kb)

        if host.frame_rgb888 and host.frame_w > 0:
            try:
                surf = pygame.image.frombuffer(host.frame_rgb888, (host.frame_w, host.frame_h), "RGB").copy()
                scr.blit(pygame.transform.smoothscale(surf, (inner.w, inner.h)), inner.topleft)
            except Exception as e:
                scr.blit(fb.render(str(e), True, TEXT), (inner.x + 6, inner.y + 6))
        else:
            scr.blit(fb.render("ROM auto-downloads snes9x core if missing", True, TEXT_DIM), (inner.x + 10, inner.y + 12))

        pygame.draw.rect(scr, BTN_BG, side, border_radius=4)
        pygame.draw.rect(scr, EDGE, side, 1, border_radius=4)
        sy = side.y + 8
        scr.blit(ft.render("Cart", True, TEXT), (side.x + 8, sy))
        sy += 22
        scr.blit(fs.render(cart or "(no ROM)", True, TEXT_DIM), (side.x + 8, sy))
        sy += 20
        scr.blit(ft.render("Log", True, TEXT), (side.x + 8, sy))
        sy += 18
        for ln in host.log[-9:]:
            scr.blit(fs.render(ln[:42], True, TEXT_DIM), (side.x + 8, sy))
            sy += 13

        btn(scr, fb, "Core", b1, (mx, my))
        btn(scr, fb, "DL core", b2, (mx, my), on=not dl.get("busy", False))
        btn(scr, fb, "ROM", b3, (mx, my), on=_can_pick_files())
        btn(scr, fb, "||" if not pause else ">", b4, (mx, my), on=host.rom_loaded)
        btn(scr, fb, "Rst", b5, (mx, my), on=host.rom_loaded)
        btn(scr, fb, "Aud", b6, (mx, my), on=mix)
        btn(scr, fb, "X", b7, (mx, my), on=host.loaded)

        if host.rom_loaded and not pause:
            try:
                host.run_frame()
            except Exception as e:
                toast_msg(str(e), 200)
                traceback.print_exc()
                host.unload()
                kb.clear()
            if mix and ch and not mute and host.audio_buffer:
                try:
                    snd = pygame.mixer.Sound(buffer=bytes(host.audio_buffer))
                    if not ch.get_busy():
                        ch.play(snd)
                    elif ch.get_queue() is None:
                        ch.queue(snd)
                except Exception:
                    pass

        if tn > 0:
            tn -= 1
            s = fs.render(toast, True, TEXT)
            pygame.draw.rect(scr, (0, 0, 0), (8, scr.get_height() - 32, s.get_width() + 14, 26), border_radius=6)
            scr.blit(s, (12, scr.get_height() - 28))

        pygame.display.flip()
        clk.tick(TARGET_FPS)

    host.unload()
    pygame.quit()
    if root:
        try:
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    main()
