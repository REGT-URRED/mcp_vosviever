"""MCP server that drives the local VOSviewer.exe desktop application.

VOSviewer has two disjoint execution paths, and that is the whole design here:

* BATCH (`vos_analyze`). If argv contains at least one `save_*` parameter,
  VOSviewer runs the job, writes every requested output and EXITS BY ITSELF with
  code 0. Measured on this machine: a full map+network+layout+clustering+4 saves
  pipeline finished in 12.0s. So batch is a plain subprocess call; there is no
  polling loop, no kill and no sleep-and-hope.
* INTERACTIVE (`vos_open`, `vos_capture`, `vos_ui`, `vos_close`). Without any
  `save_*` parameter VOSviewer stays open and shows its GUI. Measured: a bare
  launch and a `-map <abs>` launch both stayed alive past 10s. So the interactive
  path is Popen, stays alive, and is killed explicitly on close.

The two paths are never blended: `vos_open` rejects any `save_*` flag, and
`vos_analyze` refuses to run without at least one.

Every argv is built from a hard whitelist (SPECS). VOSviewer's parser is
undocumented and forgiving in a dangerous way: any value token not preceded by a
valid flag raises a MODAL dialog titled `Error` reading
`Program argument '<token>' is invalid.` and then hangs forever with no stdout
trace. Building argv from a whitelist and always emitting flag-before-value is
the only reliable guard; `_error_dialog_text` is the second line of defence.

`VOSviewer.exe` is a launcher, not the app: it has no window and an empty
MainWindowTitle, and the real Swing GUI runs in a CHILD `javaw.exe`. Measured:
launcher 25892 -> javaw 15488 owning `SunAwtFrame "VOSviewer - journal_map.txt"`.
So every window lookup and every teardown walks the process tree
(`_vosviewer_pids`), and killing only the launcher orphans the javaw.exe.
"""

from __future__ import annotations

import asyncio
import csv
import ctypes
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from ctypes import wintypes
from typing import Any

from PIL import Image

from mcp.server import MCPServer

VOSVIEWER_EXE = os.environ.get(
    "VOSVIEWER_EXE", r"C:\Users\TOOR\Downloads\actividad\hoy\VOSviewer.exe"
)
DEFAULT_OUT_DIR = os.environ.get(
    "VOSVIEWER_OUT_DIR", r"C:\Users\TOOR\Downloads\actividad\hoy\vosviewer_out"
)

STATE: dict[str, Any] = {
    "proc": None,
    "pid": None,  # VOSviewer.exe launcher pid
    "started": None,
    "args": None,
    "pids": [],  # whole tree, launcher first: [_vosviewer_pids]
    "gui_pid": None,  # javaw.exe that actually owns the Swing windows
}


def _reset_state() -> None:
    """Deja el estado global como si nunca se hubiera abierto nada."""
    STATE.update(
        {
            "proc": None,
            "pid": None,
            "started": None,
            "args": None,
            "pids": [],
            "gui_pid": None,
        }
    )

# ------------------------------------------------------------------ argv specs

_INPUT_FLAGS = [
    "gml",
    "json",
    "map",
    "network",
    "pajek_network",
    "pajek_partition",
    "pajek_vector",
    "corpus",
    "scores",
    "thesaurus",
    "cluster_colors",
    "density_colors",
    "overlay_colors",
]
_CORPUS_FLAGS = ["counting_method", "min_n_occurrences", "n_terms"]
_SAVE_FLAGS = [
    "save_gml",
    "save_json",
    "save_map",
    "save_network",
    "save_pajek_network",
    "save_pajek_partition",
    "save_pajek_vector",
]
_SHOT_FLAGS = [
    f"save_screenshot_{e}"
    for e in ("bmp", "emf", "eps", "gif", "jpg", "pdf", "png", "svg", "swf", "tiff")
]
_LAYOUT_SPEC = {
    "attraction": "int",
    "repulsion": "int",
    "resolution": "float",
    "min_cluster_size": "int",
    "merge_small_clusters": "bool",
    "run_layout": "flag",
    "run_clustering": "flag",
    "skip_clustering": "flag",
    "largest_component": "flag",
}
_VIS_SPEC = {
    "network_visualization": "flag",
    "overlay_visualization": "flag",
    "density_visualization": "flag",
    "density": "int",
    "circles_frames": "int",
    "black_background": "bool",
    "white_background": "bool",
    "colored_lines": "bool",
    "curved_lines": "bool",
    "scale": "float",
    "kernel_width": "float",
    "label_size_variation": "float",
    "line_size_variation": "float",
    "max_label_length": "int",
    "max_n_lines": "int",
    "min_line_strength": "float",
    "min_score": "float",
    "max_score": "float",
    "scores_normalization": "int",
}
_CAMERA_SPEC = {"zoom_level": "zoom", "show_item": "str"}
_INTER_SPEC = {
    "pan_step_size": "float",
    "zoom_speed": "float",
    "zoom_step_size": "float",
}
_MISC_SPEC = {"encoding": "str", "file_location": "str"}

ENUM_FLAGS = {
    "counting_method": {1, 2},
    "density": {1, 2},
    "circles_frames": {1, 2},
    "scores_normalization": {1, 2, 3, 4},
}

SPECS: dict[str, tuple[str, str]] = {}
for _k in _INPUT_FLAGS:
    SPECS[_k] = ("path", "input")
for _k in _CORPUS_FLAGS:
    SPECS[_k] = ("int", "corpus")
for _k in _SAVE_FLAGS + _SHOT_FLAGS:
    SPECS[_k] = ("path", "save")
for _d, _s in (
    (_LAYOUT_SPEC, "layout"),
    (_VIS_SPEC, "visualization"),
    (_CAMERA_SPEC, "camera"),
    (_INTER_SPEC, "interaction"),
    (_MISC_SPEC, "misc"),
):
    for _k, _v in _d.items():
        SPECS[_k] = (_v, _s)

_ARGV_ORDER = (
    _INPUT_FLAGS
    + _CORPUS_FLAGS
    + list(_LAYOUT_SPEC)
    + list(_VIS_SPEC)
    + list(_CAMERA_SPEC)
    + list(_INTER_SPEC)
    + list(_MISC_SPEC)
    + _SAVE_FLAGS
    + _SHOT_FLAGS
)
_NEEDS_CORPUS = {
    "counting_method",
    "min_n_occurrences",
    "n_terms",
    "scores",
    "thesaurus",
}
_NEEDS_PAJEK = {"pajek_partition", "pajek_vector"}
_EXCLUSIVE_INPUTS = ("map", "pajek_network", "gml", "json", "corpus")

# ---------------------------------------------------------------- file parsing


def _read_text(path: str) -> str:
    """VOSviewer writes a UTF-8 BOM; older exports can carry latin-1 payloads."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _attr_parts(header: str) -> tuple[str, str]:
    m = re.match(r"^(weight|score)<(.+)>$", header)
    return (m.group(1), m.group(2)) if m else ("", header)


_PLAIN_COLS = {"id", "label", "description", "x", "y", "cluster", "framerate"}


def parse_map_txt(path: str) -> dict[str, Any]:
    """Header-driven map parser: attribute names are read from the header, never fixed."""
    lines = [ln for ln in _read_text(path).splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty map file")
    header = lines[0].split("\t")
    items: list[dict[str, Any]] = []
    for raw in lines[1:]:
        cols = raw.split("\t")
        if len(cols) < len(header):
            cols += [""] * (len(header) - len(cols))
        item: dict[str, Any] = {
            "id": cols[0].strip(),
            "label": "",
            "cluster": "",
            "x": None,
            "y": None,
            "description": "",
            "values": {},
            "scores": {},
        }
        for col, val in zip(header, cols):
            kind, name = _attr_parts(col)
            key, lname = _norm(name), _norm(col)
            if lname in ("label", "description", "cluster", "id"):
                item[lname] = val
            elif lname == "x":
                item["x"] = _as_float(val)
            elif lname == "y":
                item["y"] = _as_float(val)
            elif kind == "score":
                f = _as_float(val)
                item["scores"][name] = f
                if f is not None:  # score attributes are sortable too, not display-only
                    item["values"][key] = f
            elif lname not in _PLAIN_COLS:
                f = _as_float(val)
                if f is not None:
                    item["values"][key] = f
        items.append(item)
    return {"items": items, "links": [], "format": "map"}


def parse_network_txt(path: str) -> dict[str, Any]:
    links: list[dict[str, Any]] = []
    nodes: set[str] = set()
    for raw in _read_text(path).splitlines():
        cols = raw.split()
        if len(cols) < 2:
            continue
        nodes.update(cols[:2])
        strength = _as_float(cols[2]) if len(cols) > 2 else 1.0
        links.append(
            {
                "source_id": cols[0],
                "target_id": cols[1],
                "strength": 1.0 if strength is None else strength,
            }
        )
    return {"items": [], "links": links, "node_count": len(nodes), "format": "network"}


def parse_json_map(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        doc = json.load(fh)
    net = doc.get("network", doc) if isinstance(doc, dict) else {}
    items: list[dict[str, Any]] = []
    for raw in net.get("items", []):
        values = {}
        for name, val in (raw.get("weights") or {}).items():
            f = _as_float(val)
            if f is not None:
                values[_norm(name)] = f
        items.append(
            {
                "id": str(raw.get("id", "")),
                "label": raw.get("label", ""),
                "cluster": str(raw.get("cluster", "")),
                "x": raw.get("x"),
                "y": raw.get("y"),
                "description": raw.get("description", ""),
                "values": values,
                "scores": dict(raw.get("scores") or {}),
            }
        )
    return {"items": items, "links": net.get("links", []) or [], "format": "json"}


_METRIC_ALIASES = {
    "weight": "total_link_strength",
    "link_strength": "total_link_strength",
    "total_link_strength": "total_link_strength",
    "links": "links",
    "citations": "citations",
    "links_average": "links_average",
}


def _resolve_metric(items: list[dict[str, Any]], metric: str) -> str:
    available: set[str] = set()
    for it in items:
        available.update(it["values"])
    key = _norm(metric)
    for cand in (key, _METRIC_ALIASES.get(key, "")):
        if cand in available:
            return cand
    raise ValueError(
        f"metric '{metric}' not present in file; available: {sorted(available)}"
    )


def _file_format(path: str) -> str:
    if path.lower().endswith((".json", ".net", ".clu", ".vec")):
        return "json" if path.lower().endswith(".json") else "network"
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        first = fh.readline().strip()
    cols = first.split("\t")
    return "map" if len(cols) >= 4 and cols[0].strip() == "id" else "network"


def _item_view(it: dict[str, Any], value: float, rank: int) -> dict[str, Any]:
    v = it["values"]
    return {
        "rank": rank,
        "id": it["id"],
        "label": it["label"],
        "cluster": it["cluster"],
        "weight": value,
        "total_link_strength": v.get("total_link_strength"),
        "links": v.get("links"),
        "x": it["x"],
        "y": it["y"],
        "scores": it["scores"],
    }


# ------------------------------------------------------------------ win32 glue

user32, gdi32, kernel32 = (
    ctypes.windll.user32,
    ctypes.windll.gdi32,
    ctypes.windll.kernel32,
)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WM_CLOSE, WM_GETTEXT = 0x0010, 0x000D
SW_RESTORE, SRCCOPY, PW_RENDERFULLCONTENT, DIB_RGB_COLORS = 9, 0x00CC0020, 0x00000002, 0
PROCESS_QUERY_LIMITED_INFORMATION, TH32CS_SNAPPROCESS = 0x1000, 0x00000002
_PROCESS_COMMAND_LINE = 60  # NtQueryInformationProcess class
_STILL_ACTIVE = 259

# Without explicit types ctypes passes handles as 32-bit ints and truncates them.
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]

MOUSEEVENTF = {
    "move": 0x0001,
    "ldown": 0x0002,
    "lup": 0x0004,
    "rdown": 0x0008,
    "rup": 0x0010,
    "mdown": 0x0020,
    "mup": 0x0040,
    "wheel": 0x0800,
    "absolute": 0x8000,
}
KEYEVENTF_KEYUP, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0001
VK_NAMES = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "f1": 0x70,
}
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", ctypes.c_byte * 8)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class PROCESSENTRY32W(ctypes.Structure):
    """Toolhelp32 snapshot row: the only cheap parent-pid map on Windows."""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]


def _window_info(hwnd: int) -> dict[str, Any]:
    buf, cls = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 512)
    user32.GetClassNameW(hwnd, cls, 256)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "hwnd": int(hwnd),
        "title": buf.value,
        "class": cls.value,
        "rect": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
        "visible": bool(user32.IsWindowVisible(hwnd)),
    }


def _windows_for_pids(pids: int | list[int]) -> list[dict[str, Any]]:
    """Every top-level window owned by ANY pid in the process tree.

    VOSviewer.exe owns none of them: the Swing GUI runs in a child javaw.exe, so
    scanning the launcher pid alone finds nothing at all.
    """
    wanted = {int(pids)} if isinstance(pids, int) else {int(p) for p in pids}
    out: list[dict[str, Any]] = []

    def cb(hwnd: int, _l: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in wanted:
            info = _window_info(hwnd)
            info["pid"] = owner.value
            out.append(info)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _process_image(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        kernel32.CloseHandle(handle)


def _process_command_line(pid: int) -> str:
    """QueryFullProcessImageNameW only yields the image path, so argv needs the PEB.

    The javaw.exe that hosts VOSviewer is launched with the VOSviewer exe path plus
    the job arguments, which is the reliable way to tell our GUI from any other
    javaw.exe on the machine (and to rule out pid reuse).
    """
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        ntdll, size = ctypes.windll.ntdll, ctypes.c_ulong(0)
        if not ntdll.NtQueryInformationProcess.restype:
            ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        ntdll.NtQueryInformationProcess(
            handle, _PROCESS_COMMAND_LINE, None, 0, ctypes.byref(size)
        )
        if not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(
            handle, _PROCESS_COMMAND_LINE, buf, size.value, ctypes.byref(size)
        ):
            return ""
        length = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort)).contents.value
        # UNICODE_STRING: Length@0, MaximumLength@2, Buffer@sizeof(void*) (self-relative)
        pointer = ctypes.cast(
            ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ).contents.value
        return ctypes.wstring_at(pointer, length // 2) if pointer and length else ""
    except OSError:
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _proc_snapshot() -> list[tuple[int, int, str]]:
    """(pid, ppid, exe) for every live process on the machine."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snap or snap == invalid:
        return []
    entry, rows = PROCESSENTRY32W(), []
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        rows.append(
            (int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile)
        )
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return rows


def _process_children(pid: int) -> list[int]:
    """Every descendant of pid, breadth-first, launcher root excluded."""
    kids: dict[int, list[int]] = {}
    for child, parent, _exe in _proc_snapshot():
        kids.setdefault(parent, []).append(child)
    ordered, seen, queue = [], set(), [int(pid)]
    while queue:
        for child in kids.get(queue.pop(0), ()):
            if child not in seen:
                seen.add(child)
                ordered.append(child)
                queue.append(child)
    return ordered


def _is_gui_child(pid: int) -> bool:
    """True for a live javaw.exe whose command line still points at VOSviewer."""
    if os.path.basename(_process_image(pid)).lower() != "javaw.exe":
        return False
    return "vosviewer" in _process_command_line(pid).lower()


def _vosviewer_pids() -> list[int]:
    """Launcher pids first, then their live javaw.exe GUI children.

    Measured on this machine: VOSviewer.exe is only a launcher (no window, empty
    MainWindowTitle); the real Swing GUI is a child javaw.exe titled
    "VOSviewer - <file>". Window lookups and teardown must therefore walk the tree,
    and a leftover launcher is still seeded so orphans from earlier runs are seen.
    """
    seeds: list[int] = []
    for pid in [STATE.get("pid"), *(STATE.get("pids") or [])]:
        if pid and int(pid) not in seeds and _pid_alive(int(pid)):
            seeds.append(int(pid))
    for pid, _ppid, exe in _proc_snapshot():
        if pid not in seeds and exe.lower() == "vosviewer.exe":
            seeds.append(pid)
    ordered = list(seeds)
    # La lista se recuerda para que una corrida posterior encuentre un launcher
    # huerfano. Es una cache explicita, no un efecto colateral.
    STATE["pids"] = ordered
    for seed in seeds:
        for child in _process_children(seed):
            if child not in ordered and _pid_alive(child) and _is_gui_child(child):
                ordered.append(child)
    STATE["pids"] = ordered
    return ordered


def _alive_pids() -> list[int]:
    return [p for p in _vosviewer_pids() if _pid_alive(p)]


def _pid_alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return code.value == _STILL_ACTIVE


def _error_dialog_text(pids: int | list[int]) -> str | None:
    """A modal 'Error' dialog blocks the AWT thread forever; fail fast instead."""
    for info in _windows_for_pids(pids):
        if info["title"].strip() != "Error":
            continue
        texts = [info["title"]]

        def cb(child: int, _l: int) -> bool:
            buf = ctypes.create_unicode_buffer(1024)
            user32.SendMessageW(child, WM_GETTEXT, 1024, ctypes.byref(buf))
            if buf.value.strip():
                texts.append(buf.value.strip())
            return True

        user32.EnumChildWindows(info["hwnd"], WNDENUMPROC(cb), 0)
        return " | ".join(dict.fromkeys(texts))
    return None


_DIALOG_DIR = os.path.join(DEFAULT_OUT_DIR, "dialogs")

# A modal that asks something routine is answered by policy; anything unrecognised
# is captured and reported instead of guessed, because guessing can destroy output.
_DIALOG_RULES = (
    # "error" va primero: un dialogo que dice "Error ... replace" no se
    # autoconfirma con Yes.
    ("error", "stop"),
    ("replace", "yes"),
    ("overwrite", "yes"),
    ("already exists", "yes"),
    ("about", "cancel"),
)
_DIALOG_DEFAULT = "stop"

# VOSviewer shows transient progress windows while it computes. They close by
# themselves and must never be answered or treated as a blocker. Matched by exact
# title, because "Advanced Layout Parameters" is a real dialog that contains "layout".
_PROGRESS_TITLES = frozenset(
    {
        "layout",
        "clustering",
        "term co-occurrence map",
        "co-occurrence map",
        "loading",
        "saving",
        "saving map",
        "saving network",
        "saving screenshot",
        "computing",
    }
)


def _dialog_choice(title: str) -> str:
    low = (title or "").lower()
    for needle, choice in _DIALOG_RULES:
        if needle in low:
            return choice
    return _DIALOG_DEFAULT


def _modal_windows(pids: int | list[int]) -> list[dict[str, Any]]:
    """Java modal dialogs are SunAwtDialog; the map itself is SunAwtFrame."""
    visible = [w for w in _windows_for_pids(pids) if w["visible"] and w["rect"][2] > 0]
    dialogs = [w for w in visible if w["class"] == "SunAwtDialog"]
    if dialogs:
        return dialogs
    # Fallback: a real dialog is topmost, titled and is not the main frame or the
    # toolkit. Testing GetWindowLongW(-20) as a boolean is wrong: that is the whole
    # GWL_EXSTYLE bitmask and is non-zero for almost every window.
    frames = {"SunAwtFrame", "SunAwtToolkit"}
    return [
        w
        for w in visible
        if user32.GetWindowLongW(w["hwnd"], -20) & WS_EX_TOPMOST
        and w.get("class") not in frames
        and (w.get("title") or "").strip()
    ]


def _capture_window_png(hwnd: int, path: str) -> tuple[bool, str]:
    try:
        info = _window_info(hwnd)
        image, method = _grab(hwnd, info["rect"])
    except Exception:
        return False, "failed"
    if image is None:
        return False, method
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.save(path)
    return True, method


def _send_choice(hwnd: int, choice: str) -> None:
    """Answer a modal by clicking its button. Enter's default is unreliable in JOptionPane."""
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.25)
    if choice in ("yes", "ok", "accept"):
        info = _window_info(hwnd)
        left, top = info["rect"][0], info["rect"][1]
        width = max(1, info["rect"][2])
        height = max(1, info["rect"][3])
        # VOSviewer's confirmation dialog has the affirmative on the LEFT.
        x = left + int(width * 0.22)
        y = top + int(height * 0.68)
        user32.SetCursorPos(x, y)
        time.sleep(0.12)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.08)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    else:
        vk = 0x1B
        _send([_key_input(vk), _key_input(vk, up=True)])


def _answer_dialog(win: dict[str, Any], pids: int | list[int]) -> dict[str, Any]:
    """Capture a dialog, decide by policy, answer it, and record what was done."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shot = os.path.join(_DIALOG_DIR, f"dialog_{stamp}_{win['hwnd']}.png")
    captured, method = _capture_window_png(win["hwnd"], shot)
    title = win.get("title") or ""
    choice = _dialog_choice(title)
    if choice != "stop":
        _send_choice(win["hwnd"], choice)
    return {
        "hwnd": win["hwnd"],
        "title": title,
        "class": win.get("class"),
        "action": choice,
        "screenshot": shot if captured else None,
        "capture_method": method,
    }


WS_EX_TOPMOST = 0x00000008  # GetWindowLongW(hwnd, GWL_EXSTYLE) bit


def _live_vos_pids() -> list[int]:
    """Every live pid whose image is VOSviewer or its javaw.exe GUI child."""
    out: list[int] = []
    for pid, _parent, name in _proc_snapshot():
        low = (name or "").lower()
        if "vosviewer" in low or low.startswith("javaw"):
            out.append(pid)
    return out


def _vosviewer_only_pids() -> list[int]:
    """Pids que son VOSviewer o su hijo javaw.exe, sin mezclar otros Java.

    _live_vos_pids acepta cualquier javaw.exe de la maquina: con eso, un
    vos_dialog podia clicar el boton de otra aplicacion Java.
    """
    return _vosviewer_pids()


def _kill_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30
    )


_AWT_CLASSES = ("SunAwtFrame", "SunAwtDialog", "SunAwtToolkit")


def _main_window(
    pids: int | list[int], target: str | None
) -> tuple[int, dict[str, Any]]:
    """Resolve one window across the whole tree: hwnd, then class, then any window."""
    if target and str(target).isdigit():
        hwnd = int(target)
        return hwnd, _window_info(hwnd)
    visible = [w for w in _windows_for_pids(pids) if w["visible"] and w["rect"][2] > 0]
    if target == "active":
        fg = int(user32.GetForegroundWindow())
        if any(w["hwnd"] == fg for w in visible):
            return fg, _window_info(fg)
    if target == "modal":  # a Java dialog is a topmost window owned by the same pid
        topmost = [
            w for w in visible if user32.GetWindowLongW(w["hwnd"], -20) & WS_EX_TOPMOST
        ]
        if topmost:
            return int(topmost[0]["hwnd"]), topmost[0]
    if not visible:
        raise RuntimeError(f"no visible VOSviewer window for pids {pids}")
    pool = (
        [w for w in visible if w["class"] == "SunAwtFrame"]
        or [w for w in visible if w["class"] in _AWT_CLASSES]
        or visible
    )
    main = max(pool, key=lambda w: w["rect"][2] * w["rect"][3])
    return int(main["hwnd"]), main


# ------------------------------------------------------------ argv validation


def _coerce(flag: str, value: Any) -> str:
    kind = SPECS[flag][0]
    if kind == "flag":
        return ""
    if kind == "bool":
        return (
            value.lower() if isinstance(value, str) else ("true" if value else "false")
        )
    if kind in ("int", "zoom"):
        try:
            num = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{flag} expects an integer, got {value!r}")
        if flag in ENUM_FLAGS and num not in ENUM_FLAGS[flag]:
            raise ValueError(
                f"{flag} must be one of {sorted(ENUM_FLAGS[flag])}, got {num}"
            )
        if kind == "zoom" and num < 1:
            raise ValueError(f"zoom_level must be >= 1, got {num}")
        return str(num)
    if kind == "float":
        try:
            return repr(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"{flag} expects a number, got {value!r}")
    if kind == "str":
        return str(value)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{flag} expects a non-empty path string")
    return value


def build_argv(groups: dict[str, dict[str, Any]], require_saves: bool) -> list[str]:
    """Flatten whitelisted groups into a deterministic '-flag value' argv."""
    flat: dict[str, Any] = {}
    for section, values in groups.items():
        for flag, value in (values or {}).items():
            if flag not in SPECS:
                raise ValueError(
                    f"unknown flag '{flag}' in {section}; allowed: {sorted(SPECS)}"
                )
            if SPECS[flag][1] != section:
                raise ValueError(
                    f"flag '{flag}' belongs to '{SPECS[flag][1]}', not '{section}'"
                )
            if value is not None:
                flat[flag] = value
    clash = [f for f in _EXCLUSIVE_INPUTS if f in flat]
    if len(clash) > 1:
        raise ValueError(f"these input flags are mutually exclusive: {clash}")
    if flat.keys() & _NEEDS_CORPUS and "corpus" not in flat:
        raise ValueError(
            f"{sorted(flat.keys() & _NEEDS_CORPUS)} require the 'corpus' flag"
        )
    if flat.keys() & _NEEDS_PAJEK and "pajek_network" not in flat:
        raise ValueError(
            f"{sorted(flat.keys() & _NEEDS_PAJEK)} require 'pajek_network'"
        )
    saves = [f for f in flat if SPECS[f][1] == "save"]
    if require_saves and not saves:
        raise ValueError(
            "batch mode needs at least one save_* output, otherwise VOSviewer "
            "stays open instead of exiting; use vos_open for interactive mode"
        )
    if not require_saves and saves:
        raise ValueError(
            f"interactive mode must not pass {saves}: any save_* makes VOSviewer "
            "exit immediately; use vos_analyze for batch mode"
        )
    argv: list[str] = []
    for flag in _ARGV_ORDER:
        if flag not in flat:
            continue
        if SPECS[flag][0] == "flag":
            argv.append(f"-{flag}")  # bare token, no value
        else:
            argv.extend([f"-{flag}", _coerce(flag, flat[flag])])
    return argv


def _absolute(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


# ------------------------------------------------------------------- run core


def _java_home() -> str | None:
    """Runtime de Java que se le va a pasar a VOSviewer.

    Se busca en VOSVIEWER_JAVA_HOME y despues en JAVA_HOME. VOSviewer 1.6.x
    necesita Java 8: con Java 9 o superior el modo batch revienta con una
    excepcion de Java y no escribe ninguna salida.
    """
    for nombre in ("VOSVIEWER_JAVA_HOME", "JAVA_HOME"):
        valor = os.environ.get(nombre)
        if valor and os.path.isdir(valor):
            return valor
    return None


def _java_version(java_home: str | None = None) -> tuple[int, int] | None:
    """Version de Java del runtime indicado, o None si no se puede determinar."""
    casa = java_home if java_home is not None else _java_home()
    java = os.path.join(casa, "bin", "java.exe") if casa else shutil.which("java")
    if not java or not os.path.isfile(java):
        return None
    try:
        salida = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=25
        )
    except (OSError, subprocess.SubprocessError):
        return None
    texto = (salida.stderr or "") + (salida.stdout or "")
    coincide = re.search(r'version "(\d+)\.(\d+)', texto)
    if not coincide:
        return None
    return int(coincide.group(1)), int(coincide.group(2))


def _java_problem() -> str:
    """Descripcion del problema de Java, o cadena vacia si el runtime sirve."""
    version = _java_version()
    if version is None:
        return ""
    if version[0] >= 9:
        return (
            "VOSviewer 1.6.x necesita Java 8 y el runtime disponible es Java %d.%d. "
            "El modo batch falla con una excepcion de Java y no escribe ninguna "
            "salida. Indica un JRE 8 en VOSVIEWER_JAVA_HOME o en JAVA_HOME."
            % version
        )
    return ""


def _vosviewer_env() -> dict[str, str]:
    """Entorno para los procesos VOSviewer, con el runtime de Java resuelto."""
    env = dict(os.environ)
    casa = _java_home()
    if casa:
        env["JAVA_HOME"] = casa
        env["PATH"] = os.path.join(casa, "bin") + os.pathsep + env.get("PATH", "")
    return env


def _low_integrity_label(dir_path: str) -> bool:
    """Baja la etiqueta de integridad de una carpeta a Low.

    El proceso javaw.exe de VOSviewer corre con Low Mandatory Level. Un proceso
    de integridad baja no puede escribir en una carpeta de integridad media:
    Windows lo bloquea y Java lo reporta como "Acceso denegado" dentro de un
    dialogo modal que el usuario tiene que cerrar a mano. Las carpetas que crea
    el usuario con su cuenta normal quedan en Medium, asi que hay que replicar
    la etiqueta Low antes de cada corrida.
    """
    if os.name != "nt" or not dir_path or not os.path.isdir(dir_path):
        return False
    try:
        proc = subprocess.run(
            ["icacls", dir_path, "/setintegritylevel", "(OI)(CI)L"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _prepare_out_dir(path: str, problemas: list[str] | None = None) -> str:
    """Deja la carpeta de salida lista para que VOSviewer pueda escribir en ella.

    Crea la carpeta si falta y le baja la etiqueta de integridad. Si la etiqueta
    no se puede aplicar, VOSviewer va a mostrar "Acceso denegado", asi que el
    motivo se anota en `problemas` en vez de perderse.
    """
    full = _absolute(path)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
        if not _low_integrity_label(parent):
            if problemas is not None:
                problemas.append(
                    "no se pudo bajar la etiqueta de integridad de %s: VOSviewer "
                    "corre con integridad baja y no va a poder escribir ahi "
                    "(va a mostrar 'Acceso denegado')" % parent
                )
    return full


def _build_inputs(
    source: str,
    source_path: str | None,
    network_path: str | None = None,
    thesaurus_path: str | None = None,
    scores_path: str | None = None,
) -> dict[str, str]:
    """Entradas de VOSviewer, sin que una pise a otra.

    Con source='network' y network_path a la vez el source_path se perdia en
    silencio; ahora es un error explicito.
    """
    entradas: dict[str, str] = {}
    if source_path:
        entradas[source] = _absolute(source_path)
    for flag, path in (
        ("network", network_path),
        ("thesaurus", thesaurus_path),
        ("scores", scores_path),
    ):
        if not path:
            continue
        if flag in entradas:
            raise ValueError(
                "el archivo de %s esta definido dos veces: como source y como %s" % (flag, flag)
            )
        entradas[flag] = _absolute(path)
    return entradas


def _validate_inputs(entradas: dict[str, str]) -> list[str]:
    """Todo lo que haria fallar a VOSviewer con un dialogo modal, detectado antes."""
    problemas: list[str] = []
    for flag, path in entradas.items():
        if not os.path.isfile(path):
            problemas.append("el archivo de %s no existe: %s" % (flag, path))
    if problemas:
        return problemas
    if "map" in entradas:
        problemas += _validate_map_file(entradas["map"])
    if "network" in entradas:
        problemas += _validate_network_file(entradas["network"])
    return problemas


def _recover_screenshot(
    mapa: str | None,
    red: str | None,
    layout: dict[str, Any] | None,
    visualization: dict[str, Any] | None,
    destino: str,
    max_wait_s: float = 45.0,
) -> dict[str, Any]:
    """Saca una captura real de la ventana de VOSviewer.

    El flag -save_screenshot_png no sirve en modo batch: en esta maquina aborta
    la corrida entera y deja el archivo en 0 bytes. La via que funciona es
    abrir la aplicacion en modo interactivo con el mapa ya calculado, esperar
    a que el render se estabilice y capturar la ventana.

    Se mide la estabilidad comparando dos capturas consecutivas: cuando el PNG
    deja de cambiar, el render termino. Ademas se exige que la imagen tenga
    contenido, porque una ventana sin pintar tambien da dos capturas iguales.
    """
    if not mapa or not os.path.isfile(mapa):
        return {
            "ok": False,
            "error": "no hay ningun mapa para capturar: la corrida batch no guardo "
            "save_map y no se paso source_path",
        }
    destino = _absolute(destino)
    _prepare_out_dir(destino)
    temporal = os.path.join(os.path.dirname(destino), ".captura_en_curso.png")
    inicio = time.monotonic()
    intentos = 0
    previo: str | None = None
    try:
        vos_close()
        abierto = vos_open(
            source="map",
            source_path=mapa,
            network_path=red,
            layout=layout or {},
            visualization=visualization or {},
        )
        if not abierto.get("ok"):
            return {
                "ok": False,
                "error": "no se pudo abrir VOSviewer para la captura: %s"
                % abierto.get("error", "sin detalle"),
            }
        while time.monotonic() - inicio < max_wait_s:
            intentos += 1
            captura = vos_capture(
                scope="window", target="active", out_path=temporal
            )
            if not captura.get("ok"):
                time.sleep(1.0)
                continue
            if not _imagen_con_contenido(temporal):
                previo = None
                time.sleep(1.2)
                continue
            actual = _sha256_file(temporal)
            if actual == previo:
                with open(temporal, "rb") as fh:
                    datos = fh.read()
                with open(destino, "wb") as fh:
                    fh.write(datos)
                return {
                    "ok": True,
                    "path": destino,
                    "bytes": len(datos),
                    "attempts": intentos,
                    "method": captura.get("method"),
                }
            previo = actual
            time.sleep(1.2)
        if os.path.isfile(temporal) and _imagen_con_contenido(temporal):
            with open(temporal, "rb") as fh:
                datos = fh.read()
            with open(destino, "wb") as fh:
                fh.write(datos)
            return {
                "ok": True,
                "path": destino,
                "bytes": len(datos),
                "attempts": intentos,
                "method": "ultima captura sin estabilizar",
            }
        return {
            "ok": False,
            "error": "la ventana no llego a pintarse en %.0fs" % max_wait_s,
            "attempts": intentos,
        }
    finally:
        vos_close()
        if os.path.isfile(temporal):
            try:
                os.unlink(temporal)
            except OSError:
                pass


def _imagen_tiene_contenido(imagen: Any) -> bool:
    """True si el render no es una imagen lisa.

    Una ventana sin pintar produce dos capturas identicas: sin esta comprobacion
    la recuperacion devolveria un PNG blanco como si fuera el mapa.
    """
    try:
        extremos = imagen.convert("L").getextrema()
    except (OSError, ValueError) as exc:
        raise RuntimeError("no se pudo leer la imagen capturada: %s" % exc) from exc
    return bool(extremos) and extremos[0] != extremos[1]


def _imagen_con_contenido(path: str) -> bool:
    """Lo mismo, sobre un archivo."""
    try:
        with Image.open(path) as imagen:
            return _imagen_tiene_contenido(imagen)
    except (OSError, ValueError) as exc:
        raise RuntimeError("no se pudo leer la captura %s: %s" % (path, exc)) from exc


def _validate_map_file(path: str) -> list[str]:
    """Problemas del mapa que harian fallar a VOSviewer con un dialogo modal.

    El caso caro es el mapa con las mismas coordenadas para todos los items:
    VOSviewer responde "All items have the same coordinates" y se queda esperando
    a que alguien cierre la ventana.
    """
    problemas: list[str] = []
    if not os.path.isfile(path):
        return ["el archivo de mapa no existe: %s" % path]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lineas = fh.read().splitlines()
    except OSError as exc:
        return ["no se pudo leer el archivo de mapa %s: %s" % (path, exc)]
    if len(lineas) < 2:
        return ["el archivo de mapa %s no tiene datos" % path]
    cabecera = lineas[0].split("\t")
    if "x" not in cabecera or "y" not in cabecera:
        return [
            "el mapa %s no tiene las columnas x e y; VOSviewer espera "
            "id, label, description, x, y, cluster, weight<...>" % path
        ]
    ix, iy = cabecera.index("x"), cabecera.index("y")
    xs: set[str] = set()
    ys: set[str] = set()
    for linea in lineas[1:]:
        campos = linea.split("\t")
        if len(campos) <= max(ix, iy):
            continue
        xs.add(campos[ix].strip())
        ys.add(campos[iy].strip())
    if len(xs) == 1 and len(ys) == 1:
        problemas.append(
            "el mapa %s tiene las mismas coordenadas x e y para todos los items; "
            "VOSviewer lo rechaza con 'All items have the same coordinates'. "
            "Asigna coordenadas distintas por item." % path
        )
    return problemas


def _validate_network_file(path: str) -> list[str]:
    """Problemas de la red. VOSviewer solo lee redes tab-delimitadas."""
    problemas: list[str] = []
    if not os.path.isfile(path):
        return ["el archivo de red no existe: %s" % path]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lineas = [l for l in fh.read().splitlines() if l.strip()]
    except OSError as exc:
        return ["no se pudo leer el archivo de red %s: %s" % (path, exc)]
    if not lineas:
        return ["el archivo de red %s esta vacio" % path]
    for numero, linea in enumerate(lineas[:5], 1):
        if "\t" not in linea:
            problemas.append(
                "la red %s no esta separada por tabulador (linea %d: %r). VOSviewer "
                "espera id1<tab>id2<tab>peso, sin cabecera" % (path, numero, linea[:40])
            )
            break
    return problemas


def _run_batch(argv: list[str], timeout_s: float) -> dict[str, Any]:
    """Batch mode: VOSviewer exits by itself; we only guard it against modal dialogs."""
    started = time.monotonic()
    preparacion: list[str] = []
    for i, token in enumerate(argv):
        if token.startswith("-") and "save" in token and i + 1 < len(argv):
            target = argv[i + 1]
            if target and not target.startswith("-"):
                # La carpeta tiene que existir y quedar con integridad baja:
                # si no, VOSviewer abre un dialogo "Acceso denegado" y cuelga.
                _prepare_out_dir(target, preparacion)
                if os.path.isfile(target):
                    try:
                        os.unlink(target)
                    except OSError:
                        pass
    argv = [VOSVIEWER_EXE] + argv  # build_argv emits flags only, never the program
    # La salida va a archivos y no a pipes: un pipe sin lector traba al hijo
    # cuando escribe mas de 64 KB y el poll lo ve vivo hasta el timeout.
    registros = tempfile.TemporaryFile(mode="w+b")
    errores = tempfile.TemporaryFile(mode="w+b")
    proc = subprocess.Popen(
        argv,
        cwd=os.path.dirname(VOSVIEWER_EXE),
        stdin=subprocess.DEVNULL,
        stdout=registros,
        stderr=errores,
        env=_vosviewer_env(),
    )
    deadline, dialog, kids = started + timeout_s, None, set()
    answered: list[dict[str, Any]] = []
    progress: set[str] = set()
    seen_dialogs: set[int] = set()
    while proc.poll() is None:
        kids.update(_process_children(proc.pid))
        tree = [*kids, proc.pid]
        for win in _modal_windows(tree):
            if win["hwnd"] in seen_dialogs:
                continue
            seen_dialogs.add(win["hwnd"])
            title = (win.get("title") or "").strip()
            if title.lower() in _PROGRESS_TITLES:
                progress.add(title)  # transient: the app is working, leave it alone
                continue
            record = _answer_dialog(win, tree)
            answered.append(record)
            if record["action"] == "stop":
                dialog = record
                break
        if dialog:
            _kill_tree(proc.pid)
            break
        if time.monotonic() > deadline:
            _kill_tree(proc.pid)
            proc.wait(timeout=10)
            return {
                "ok": False,
                "error": f"timeout after {timeout_s}s; process tree killed",
                "hint": "VOSviewer is probably sitting on a modal dialog. Raise "
                "timeout_s or inspect the argument shown in the dialog.",
                "pid": proc.pid,
                "argv": argv,
                "elapsed_s": round(time.monotonic() - started, 2),
            }
        time.sleep(0.2)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
    out = registros.read()
    err = errores.read()
    registros.close()
    errores.close()
    # The launcher may exit while its javaw.exe child is still writing outputs.
    # Killing that child in the middle leaves half-written files and an orphan
    # that the next run finds alive, so it gets a grace period first.
    limite = time.monotonic() + 6
    while time.monotonic() < limite:
        if not [p for p in kids if _pid_alive(p)]:
            break
        time.sleep(0.2)
    killed = [p for p in sorted(kids) if _pid_alive(p)]
    for pid in killed:
        _kill_tree(pid)
    if dialog:
        return {
            "ok": False,
            "error": f"VOSviewer raised a modal dialog: {dialog.get('title')}",
            "hint": (
                "an Error dialog means a flag or value was rejected; any other title "
                "means the run needs a decision. The dialog was captured to "
                f"{dialog.get('screenshot')}."
            ),
            "dialog": dialog,
            "dialogs_answered": answered,
            "progress_windows": sorted(progress),
            "argv": argv,
            "elapsed_s": round(time.monotonic() - started, 2),
        }
    return {
        # El exito se decide por el codigo de salida: sin esto, una corrida que
        # VOSviewer aborta con excepcion se reportaba como exitosa.
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "elapsed_s": round(time.monotonic() - started, 2),
        "stdout": (out or b"").decode("utf-8", "replace")[-2000:],
        "stderr": (err or b"").decode("utf-8", "replace")[-2000:],
        "killed_children": killed,
        "dialogs_answered": answered,
        "progress_windows": sorted(progress),
        "preparation_problems": preparacion,
    }


def _collect_outputs(outputs: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for flag, path in outputs.items():
        full = _absolute(path)
        entry: dict[str, Any] = {"path": full, "bytes": 0}
        if os.path.exists(full):
            entry["bytes"] = os.path.getsize(full)
            if entry["bytes"] == 0:
                entry["empty"] = True
        else:
            entry["missing"] = True
        report[flag] = entry
    return report


def _stats_from(outputs: dict[str, Any]) -> dict[str, Any]:
    ready = {}
    for k, v in outputs.items():
        if v.get("missing") or v.get("empty"):
            continue
        try:
            if os.path.getsize(v["path"]) == 0:
                continue
        except OSError:
            continue
        ready[k] = v["path"]
    if "save_json" in ready:
        doc = parse_json_map(ready["save_json"])
    elif "save_map" in ready:
        doc = parse_map_txt(ready["save_map"])
    else:
        return {}
    items = doc["items"]
    links = doc.get("links") or []
    if not links:
        net = next(
            (ready[k] for k in ("save_network", "save_pajek_network") if k in ready),
            None,
        )
        if net:
            links = parse_network_txt(net)["links"]
    clusters = {str(it["cluster"]) for it in items if str(it["cluster"])}
    sizes = sorted(
        (c, sum(1 for it in items if str(it["cluster"]) == c)) for c in clusters
    )
    return {
        "items": len(items),
        "links": len(links),
        "clusters": len(clusters),
        "cluster_sizes": [list(s) for s in sizes][:50],
    }


# ------------------------------------------------------------------ MCP tools

mcp = MCPServer(
    name="vosviewer-mcp",
    instructions=(
        "Controls the local VOSviewer.exe desktop app. vos_analyze is the batch "
        "path (writes outputs, VOSviewer exits itself); vos_open plus "
        "vos_capture/vos_ui/vos_close drive the interactive GUI. Every flag is "
        "whitelisted, and every tool returns {ok:false, error, hint} on failure."
    ),
)


def _fail(exc: BaseException, hint: str = "") -> dict[str, Any]:
    out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if hint:
        out["hint"] = hint
    return out


@mcp.tool(
    description=(
        "Batch mode: run a VOSviewer job (source map/network/corpus/gml/pajek_network/json, "
        "optional network/thesaurus/scores inputs, layout and visualization groups) that "
        "writes the requested outputs and exits by itself. outputs maps a save_* flag "
        "(save_map, save_network, save_json, save_screenshot_png, ...) to an output path; at "
        "least one is required. Returns exit code, elapsed seconds, the exact argv, output "
        "files with sizes and parsed stats (items, links, clusters)."
    )
)
def vos_analyze(
    source: str,
    source_path: str,
    outputs: dict[str, Any],
    network_path: str | None = None,
    thesaurus_path: str | None = None,
    scores_path: str | None = None,
    layout: dict[str, Any] | None = None,
    visualization: dict[str, Any] | None = None,
    camera: dict[str, Any] | None = None,
    interaction: dict[str, Any] | None = None,
    misc: dict[str, Any] | None = None,
    corpus: dict[str, Any] | None = None,
    timeout_s: float = 180,
) -> dict[str, Any]:
    """Corre una tanda en modo batch y devuelve lo que se escribio de verdad.

    Los archivos de captura no van en el argv del batch: el flag
    -save_screenshot_png aborta la corrida en esta maquina. Se sacan despues,
    de la ventana viva, que es la unica via que entrega un PNG con contenido.
    """
    try:
        if source not in _INPUT_FLAGS:
            raise ValueError(f"unknown source '{source}'; allowed: {_INPUT_FLAGS}")
        if not os.path.exists(VOSVIEWER_EXE):
            raise FileNotFoundError(f"VOSviewer not found at {VOSVIEWER_EXE}")
        problema_java = _java_problem()
        if problema_java:
            raise RuntimeError(problema_java)
        vivos = _alive_pids()
        if vivos:
            raise RuntimeError(
                "ya hay una instancia de VOSviewer corriendo (pids %s). Una corrida "
                "batch con la aplicacion abierta se queda esperando un dialogo que "
                "nunca aparece: cerrala con vos_close y volve a correr"
                % ", ".join(map(str, vivos))
            )
        entradas = _build_inputs(
            source, source_path, network_path, thesaurus_path, scores_path
        )
        problemas = _validate_inputs(entradas)
        if problemas:
            return {
                "ok": False,
                "error": "entrada invalida: " + "; ".join(problemas),
                "hint": "corrige el archivo antes de correr: VOSviewer responderia con "
                "un dialogo modal que hay que cerrar a mano",
                "problems": problemas,
            }
        pedidos = dict(outputs or {})
        capturas = {k: v for k, v in pedidos.items() if k.startswith("save_screenshot")}
        salidas = {k: v for k, v in pedidos.items() if not k.startswith("save_screenshot")}
        if not salidas and not capturas:
            raise ValueError(
                "hay que pedir al menos una salida: save_map, save_network, "
                "save_json o save_screenshot_*"
            )
        if not salidas:
            return _solo_capturas(entradas, capturas, layout, visualization)
        argv = build_argv(
            {
                "input": entradas,
                "save": dict(salidas),
                "layout": layout or {},
                "visualization": visualization or {},
                "camera": camera or {},
                "interaction": interaction or {},
                "misc": misc or {},
                "corpus": corpus or {},
            },
            require_saves=True,
        )
        result = _run_batch(argv, timeout_s)
        written = _collect_outputs(salidas)
        faltan = sorted(k for k, e in written.items() if e.get("missing"))
        if not result.get("ok") or faltan:
            # Salir con codigo 0 sin escribir lo pedido tambien es un fallo:
            # el que recibe el archivo que no existe tiene que saberlo.
            respuesta = dict(result)
            respuesta["ok"] = False
            respuesta["outputs"] = written
            respuesta["missing_outputs"] = faltan
            stderr = (result.get("stderr") or "").strip()
            if stderr:
                primera = stderr.splitlines()[0][:200]
                respuesta["hint"] = (
                    "VOSviewer salio con codigo %s y no escribio %s. Primera linea "
                    "del stderr: %s"
                    % (result.get("exit_code"), ", ".join(faltan) or "nada", primera)
                )
            elif faltan:
                respuesta["hint"] = (
                    "VOSviewer salio con codigo 0 pero no escribio: %s"
                    % ", ".join(faltan)
                )
            return respuesta
        recuperaciones: list[dict[str, Any]] = []
        mapa_calculado = written.get("save_map", {}).get("path") or entradas.get("map")
        for flag in sorted(capturas):
            r = _recover_screenshot(
                mapa_calculado,
                entradas.get("network"),
                layout,
                visualization,
                capturas[flag],
            )
            recuperaciones.append({"flag": flag, **r})
            written[flag] = {
                "path": _absolute(capturas[flag]),
                "bytes": r.get("bytes", 0),
                "recovered": bool(r.get("ok")),
            }
        vacios = sorted(k for k, e in written.items() if e.get("empty"))
        respuesta = {
            "ok": True,
            "exit_code": result["exit_code"],
            "elapsed_s": result["elapsed_s"],
            "argv": argv,
            "outputs": written,
            "stats": _stats_from(written),
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }
        if recuperaciones:
            respuesta["screenshot_recovery"] = recuperaciones
        avisos: list[str] = []
        avisos.extend(result.get("preparation_problems") or [])
        if vacios:
            avisos.append("Quedaron archivos de 0 bytes: %s" % ", ".join(vacios))
        sin_captura = [r["flag"] for r in recuperaciones if not r.get("ok")]
        if sin_captura:
            avisos.append("No se pudieron sacar las capturas: %s" % ", ".join(sin_captura))
        if avisos:
            respuesta["warnings"] = avisos
        STATE.setdefault("last_outputs", {})["analyze"] = {
            "when": time.time(),
            "argv": argv,
            "outputs": written,
        }
        return respuesta
    except Exception as exc:
        return _fail(
            exc, "only whitelisted flags are accepted; run vos_status for context"
        )


def _solo_capturas(
    entradas: dict[str, str],
    capturas: dict[str, str],
    layout: dict[str, Any] | None,
    visualization: dict[str, Any] | None,
) -> dict[str, Any]:
    """Caso de una corrida que solo pide capturas: no hay tanda que correr."""
    written: dict[str, Any] = {}
    recuperaciones: list[dict[str, Any]] = []
    for flag in sorted(capturas):
        r = _recover_screenshot(
            entradas.get("map"),
            entradas.get("network"),
            layout,
            visualization,
            capturas[flag],
        )
        recuperaciones.append({"flag": flag, **r})
        written[flag] = {
            "path": _absolute(capturas[flag]),
            "bytes": r.get("bytes", 0),
            "recovered": bool(r.get("ok")),
        }
    return {
        "ok": bool(recuperaciones) and all(r.get("ok") for r in recuperaciones),
        "elapsed_s": 0.0,
        "argv": [],
        "outputs": written,
        "stats": {},
        "screenshot_recovery": recuperaciones,
    }

@mcp.tool(
    description=(
        "Analyze a VOSviewer map TXT, network TXT or saved JSON without launching anything. "
        "Ranks by any attribute present in the file (weight, links, citations, "
        "total_link_strength, links_average, journal_impact_factor, ...), optionally "
        "filtered by cluster_id or a label substring. Returns item/link/cluster counts, "
        "per-cluster summaries, ranked items with coordinates and scores, and the bbox."
    )
)
def vos_read(
    path: str,
    top_n: int = 20,
    metric: str = "total_link_strength",
    cluster_id: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """Pure file analysis: no process is launched."""
    try:
        full = _absolute(path)
        if not os.path.exists(full):
            raise FileNotFoundError(f"file not found: {full}")
        fmt = _file_format(full)
        doc = {"json": parse_json_map, "network": parse_network_txt}.get(
            fmt, parse_map_txt
        )(full)
        items = doc["items"]
        if not items:
            return {
                "ok": True,
                "file": full,
                "format": fmt,
                "item_count": 0,
                "link_count": len(doc.get("links") or []),
                "cluster_count": 0,
                "clusters": [],
                "items": [],
                "bbox": None,
                "hint": "network files carry links only; vos_analyze emits the map",
            }
        key = _resolve_metric(items, metric)
        selected = items
        if cluster_id not in (None, ""):
            selected = [it for it in selected if str(it["cluster"]) == str(cluster_id)]
        if search:
            needle = search.lower()
            selected = [it for it in selected if needle in str(it["label"]).lower()]
        ranked = sorted(
            selected, key=lambda it: it["values"].get(key, float("-inf")), reverse=True
        )[: max(1, int(top_n))]
        buckets: dict[str, list[dict[str, Any]]] = {}
        ordered = sorted(
            selected, key=lambda it: it["values"].get(key, float("-inf")), reverse=True
        )
        for (
            it
        ) in ordered:  # every cluster gets a summary, not only the ones in the page
            rows = buckets.setdefault(str(it["cluster"]), [])
            if len(rows) < min(max(1, int(top_n)), 5):
                rows.append(it)
        clusters = [
            {
                "id": cid,
                "size": sum(1 for it in items if str(it["cluster"]) == cid),
                "top_items": [
                    {"label": it["label"], "weight": it["values"].get(key)}
                    for it in rows
                ],
            }
            for cid, rows in sorted(buckets.items())
        ]
        xs = [it["x"] for it in items if it["x"] is not None]
        ys = [it["y"] for it in items if it["y"] is not None]
        return {
            "ok": True,
            "file": full,
            "format": fmt,
            "metric": key,
            "available_metrics": sorted({k for it in items for k in it["values"]}),
            "item_count": len(items),
            "link_count": len(doc.get("links") or []),
            "cluster_count": len({str(it["cluster"]) for it in items}),
            "clusters": clusters,
            "items": [
                _item_view(it, it["values"].get(key, 0.0), i)
                for i, it in enumerate(ranked, 1)
            ],
            "bbox": {
                "x": min(xs),
                "y": min(ys),
                "w": round(max(xs) - min(xs), 6),
                "h": round(max(ys) - min(ys), 6),
            }
            if xs and ys
            else None,
        }
    except Exception as exc:
        return _fail(exc, "metric must match an attribute present in the file header")


def _grab(hwnd: int, rect: list[int]) -> tuple[Any, str]:
    """PrintWindow first; Java Swing sometimes renders black, so verify and BitBlt."""

    left, top, width, height = rect
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, width, height)
    old = gdi32.SelectObject(hdc_mem, bmp)
    bi = BITMAPINFOHEADER()
    bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(BITMAPINFOHEADER), width, -height
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(width * height * 4)
    method = "printwindow"

    def dib() -> Any:
        gdi32.GetDIBits(hdc_mem, bmp, 0, height, buf, ctypes.byref(bi), DIB_RGB_COLORS)
        return Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", 0, 1)

    try:
        user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        img = dib()
        if not any(sum(px) > 24 for px in img.resize((32, 32)).getdata()):
            hdc_screen = user32.GetDC(0)
            gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, SRCCOPY)
            img, method = dib(), "bitblt"
            user32.ReleaseDC(0, hdc_screen)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)
    return img, method


@mcp.tool(
    description=(
        "Screenshot VOSviewer. scope='map' re-renders deterministically through the batch "
        "path (works while occluded or with no GUI session); scope='window' grabs the live "
        "window including panels via PrintWindow with PW_RENDERFULLCONTENT, falling back to "
        "BitBlt when the DIB comes back black, and reporting which method was used. "
        "scope='window' requires a live process started by vos_open."
    )
)
def vos_capture(
    scope: str = "map",
    source: str = "map",
    source_path: str | None = None,
    outputs: dict[str, Any] | None = None,
    network_path: str | None = None,
    layout: dict[str, Any] | None = None,
    visualization: dict[str, Any] | None = None,
    out_path: str | None = None,
    target: str | None = None,
    timeout_s: float = 180,
) -> dict[str, Any]:
    """map = deterministic batch render; window = live GUI grab."""
    try:
        if scope not in ("map", "window"):
            raise ValueError(f"scope must be 'map' or 'window', got {scope!r}")
        if scope == "window":
            alive = _alive_pids()
            if not alive:
                raise RuntimeError("no live VOSviewer process to capture")
            hwnd, info = _main_window(alive, target)
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.4)
                _, info = _main_window(alive, target)
            img, method = _grab(hwnd, info["rect"])
            intentos = 1
            # La ventana puede no estar pintada todavia. Una imagen lisa no es
            # una captura valida: se espera y se reintenta.
            while img is not None and not _imagen_tiene_contenido(img) and intentos < 4:
                time.sleep(1.0)
                img, method = _grab(hwnd, info["rect"])
                intentos += 1
            if img is None:
                raise RuntimeError(
                    "no se pudo capturar la ventana %s (metodo %s): VOSviewer esta "
                    "viva pero no se pudo leer su lienzo" % (info.get("title"), method)
                )
            path = (
                _absolute(out_path)
                if out_path
                else os.path.join(DEFAULT_OUT_DIR, f"capture_{int(time.time())}.png")
            )
            _prepare_out_dir(path)
            img.save(path)
            STATE.setdefault("last_outputs", {})["capture"] = {
                "when": time.time(),
                "path": path,
                "scope": scope,
            }
            return {
                "ok": True,
                "path": path,
                "bytes": os.path.getsize(path),
                "width": img.width,
                "height": img.height,
                "scope": scope,
                "method": method,
                "attempts": intentos,
                "painted": _imagen_tiene_contenido(img),
                "pids": alive,
                "pid": alive[0],
                "gui_pid": info.get("pid"),
                "hwnd": hwnd,
                "window_title": info["title"],
            }
        saves = {
            "save_screenshot_png": out_path
            or os.path.join(DEFAULT_OUT_DIR, f"capture_{int(time.time())}.png")
        }
        saves.update(outputs or {})
        result = vos_analyze(
            source=source,
            source_path=source_path or "",
            outputs=saves,
            network_path=network_path,
            layout=layout,
            visualization=visualization,
            timeout_s=timeout_s,
        )
        if not result.get("ok"):
            return result
        primary = result["outputs"].get("save_screenshot_png", {})
        respuesta = {
            "ok": True,
            "path": primary.get("path"),
            "bytes": primary.get("bytes", 0),
            "scope": "map",
            "method": "batch_render",
            "elapsed_s": result["elapsed_s"],
            "outputs": result["outputs"],
        }
        if result.get("warnings"):
            respuesta["warnings"] = result["warnings"]
        if result.get("screenshot_recovery"):
            respuesta["screenshot_recovery"] = result["screenshot_recovery"]
        return respuesta
    except Exception as exc:
        return _fail(
            exc, "scope='window' needs a live process; scope='map' needs valid inputs"
        )


@mcp.tool(
    description=(
        "Interactive mode: launch VOSviewer with the given inputs and leave it running. "
        "save_* flags are rejected because they would make it exit immediately. VOSviewer.exe "
        "is only a launcher, so this waits up to 30s for the child javaw.exe Swing window and "
        "returns the real hwnd plus launcher_pid, gui_pid, pids and every window. Follow with "
        "vos_capture scope='window', vos_ui or vos_status."
    )
)
def vos_open(
    source: str = "map",
    source_path: str | None = None,
    network_path: str | None = None,
    thesaurus_path: str | None = None,
    scores_path: str | None = None,
    layout: dict[str, Any] | None = None,
    visualization: dict[str, Any] | None = None,
    camera: dict[str, Any] | None = None,
    interaction: dict[str, Any] | None = None,
    misc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Launch and leave VOSviewer running (F2: no save_* means it stays open)."""
    started_pid = None
    try:
        if not os.path.exists(VOSVIEWER_EXE):
            raise FileNotFoundError(f"VOSviewer not found at {VOSVIEWER_EXE}")
        problema_java = _java_problem()
        if problema_java:
            raise RuntimeError(problema_java)
        if STATE.get("proc") and STATE["proc"].poll() is None:
            raise RuntimeError(f"VOSviewer already running (pid {STATE['pid']})")
        groups = {
            "layout": layout,
            "visualization": visualization,
            "camera": camera,
            "interaction": interaction,
            "misc": misc,
        }
        for section, values in groups.items():
            for flag in values or {}:
                if SPECS.get(flag, ("", ""))[1] == "save":
                    raise ValueError(
                        f"vos_open rejects '{flag}': any save_* argument makes VOSviewer "
                        "exit immediately, which would orphan the javaw.exe GUI; "
                        "use vos_analyze for batch mode"
                    )
        inputs = _build_inputs(
            source, source_path, network_path, thesaurus_path, scores_path
        )
        argv = [VOSVIEWER_EXE] + build_argv(
            {"input": inputs, **{k: (v or {}) for k, v in groups.items()}},
            require_saves=False,
        )
        proc = subprocess.Popen(
            argv,
            cwd=os.path.dirname(VOSVIEWER_EXE),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_vosviewer_env(),
        )
        started_pid = proc.pid
        STATE.update(
            {
                "proc": proc,
                "pid": proc.pid,
                "started": time.time(),
                "args": argv,
                "pids": [proc.pid],
                "gui_pid": None,
            }
        )
        deadline, frame, windows, dialog = time.time() + 30, None, [], None
        # Solo el arbol de este proceso: barrer la maquina entera podia devolver
        # la ventana de otra instancia ya abierta.
        mios: set[int] = {proc.pid}
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"VOSviewer exited immediately (code {proc.returncode}); interactive "
                    "mode must not pass any save_* argument"
                )
            mios.update(_process_children(proc.pid))
            pids = sorted(mios)
            dialog = _error_dialog_text(pids)
            if dialog:
                raise RuntimeError(f"VOSviewer raised a modal dialog: {dialog}")
            windows = _windows_for_pids(pids)
            frame = next(
                (
                    w
                    for w in windows
                    if w["class"] == "SunAwtFrame" and w["visible"] and w["rect"][2] > 0
                ),
                None,
            )
            if frame:
                break
            time.sleep(0.3)
        if not frame:
            raise RuntimeError(
                f"no SunAwtFrame appeared within 30s; tree pids={_vosviewer_pids()}, "
                f"windows={[(w['class'], w['title']) for w in windows]}"
            )
        pids = _vosviewer_pids()
        gui_pid = next((p for p in pids if p != proc.pid), None)
        STATE["gui_pid"] = gui_pid
        if not _pid_alive(proc.pid):  # F2 invariant: launcher outlives the call
            raise RuntimeError(
                f"launcher pid {proc.pid} is gone; interactive mode would leave an "
                "orphan GUI, use vos_analyze"
            )
        return {
            "ok": True,
            "pid": proc.pid,
            "launcher_pid": proc.pid,
            "gui_pid": gui_pid,
            "pids": pids,
            "hwnd": int(frame["hwnd"]),
            "title": frame["title"],
            "rect": frame["rect"],
            "class": frame["class"],
            "windows": windows,
            "args": argv,
        }
    except Exception as exc:
        if started_pid:
            _kill_tree(started_pid)
        STATE.update(
            {
                "proc": None,
                "pid": None,
                "started": None,
                "args": None,
                "pids": [],
                "gui_pid": None,
            }
        )
        return _fail(exc, "interactive mode forbids save_* flags")


@mcp.tool(
    description=(
        "Close every running VOSviewer: WM_CLOSE to all windows of the whole process tree "
        "(the Swing GUI lives in a child javaw.exe, not in VOSviewer.exe), wait 3s, then "
        "terminate every surviving pid child-first. Returns the full pid list it acted on "
        "and anything still alive afterwards."
    )
)
def vos_close() -> dict[str, Any]:
    """WM_CLOSE first, TerminateProcess only for survivors."""
    try:
        pids = _vosviewer_pids()
        windows = _windows_for_pids(pids)
        for info in windows:
            user32.PostMessageW(info["hwnd"], WM_CLOSE, 0, 0)
        deadline = time.time() + 3
        while time.time() < deadline and any(_pid_alive(p) for p in pids):
            time.sleep(0.2)
        survivors = [p for p in pids if _pid_alive(p)]
        for pid in reversed(pids):  # children first, the launcher last
            if _pid_alive(pid):
                _kill_tree(pid)
        _reset_state()
        remaining = [p for p in pids if _pid_alive(p)]
        return {
            "ok": True,
            "closed": len(pids),
            "pids": pids,
            "windows": len(windows),
            "terminated": survivors,
            "remaining": remaining,
            "windows_after": _vosviewer_pids(),
        }
    except Exception as exc:
        return _fail(exc)


@mcp.tool(
    description=(
        "Read-only VOSviewer status: running, launcher_pid, gui_pid, pids (the whole tree), "
        "main hwnd, title, rect, visible, uptime, exe path and existence, plus the windows[] "
        "array with EVERY top-level window owned by any pid in the tree (use it to target one "
        "specific window in vos_capture/vos_ui), and the artifacts written by the last "
        "vos_analyze/vos_capture."
    )
)
def vos_status() -> dict[str, Any]:
    """Everything a blind caller needs before sending coordinates."""
    try:
        pids = _vosviewer_pids()
        alive = [p for p in pids if _pid_alive(p)]
        windows = _windows_for_pids(alive)
        uptime = None
        if STATE.get("pid") and STATE.get("started"):
            uptime = round(time.time() - float(STATE["started"]), 1)
        hwnd, title, rect, visible = 0, "", [0, 0, 0, 0], False
        gui_pid = STATE.get("gui_pid")
        if alive:
            try:
                hwnd, info = _main_window(alive, None)
                title, rect, visible = info["title"], info["rect"], info["visible"]
                gui_pid = info.get("pid", gui_pid)
            except Exception:
                pass
        return {
            "ok": True,
            "running": bool(alive),
            "pid": pids[0] if pids else None,
            "launcher_pid": STATE.get("pid"),
            "gui_pid": gui_pid,
            "pids": pids,
            "alive_pids": alive,
            "hwnd": hwnd,
            "title": title,
            "rect": rect,
            "visible": visible,
            "uptime_s": uptime,
            "exe_path": VOSVIEWER_EXE,
            "exe_exists": os.path.exists(VOSVIEWER_EXE),
            "windows": windows,
            "args": STATE.get("args"),
            "last_outputs": STATE.get("last_outputs", {}),
        }
    except Exception as exc:
        return _fail(exc)


# ------------------------------------------------------------------- vos_ui


def _send(inputs: list[INPUT]) -> int:
    """Envia input. Devuelve cuantos eventos se aceptaron (0 = bloqueado)."""
    arr = (INPUT * len(inputs))(*inputs)
    enviados = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if enviados != len(inputs):
        raise RuntimeError(
            "SendInput acepto %d de %d eventos: otra aplicacion tiene el foco o "
            "el proceso corre con menor integridad" % (enviados, len(inputs))
        )
    return enviados


def _mouse(sx: int, sy: int, flag: str, data: int = 0) -> INPUT:
    inp = INPUT(type=0)
    inp.mi = MOUSEINPUT(sx, sy, data, MOUSEEVENTF[flag], 0, None)
    return inp


def _key_input(vk: int, up: bool = False) -> INPUT:
    inp = INPUT(type=1)
    flags = KEYEVENTF_KEYUP if up else 0
    scan = 0
    if vk in EXTENDED_VKS:
        scan, flags = user32.MapVirtualKeyW(vk, 0), flags | KEYEVENTF_EXTENDEDKEY
    inp.ki = KEYBDINPUT(vk, scan, flags, 0, None)
    return inp


def _vk(name: str) -> tuple[int, int]:
    """Virtual key plus VkKeyScanW shift state (1=shift, 2=ctrl, 4=alt)."""
    key = name.strip()
    if key.lower() in VK_NAMES:
        return VK_NAMES[key.lower()], 0
    if len(key) == 1:
        code = user32.VkKeyScanW(ord(key))
        if code == -1:
            raise ValueError(f"key '{name}' is not mappable on this keyboard layout")
        return (code & 0xFF), (code >> 8) & 0xFF
    raise ValueError(f"unknown key '{name}'")


def _key_sequence(text: str) -> list[INPUT]:
    seq: list[INPUT] = []
    for ch in text:
        vk, shift = _vk(ch)
        mods = [(0x10, 1), (0x11, 2), (0x12, 4)]
        held = [code for code, bit in mods if shift & bit]
        seq += [_key_input(code) for code in held]
        seq.append(_key_input(vk))
        seq += [_key_input(code, up=True) for code in reversed(held)]
    return seq


@mcp.tool(
    description=(
        "Blind coordinate input into the live VOSviewer GUI via SendInput: click, "
        "double_click, right_click, middle_click, wheel, drag, type, key. x/y are "
        "window-relative and converted through the live window rect, so a moved window "
        "cannot break them. Java Swing exposes no accessibility API, so coordinates are "
        "blind: check vos_status windows[] first and pass target=<hwnd>, 'active' or "
        "'modal' (topmost VOSviewer-owned window, e.g. a dialog). combo takes 'ctrl+s' "
        "style strings; drag goes from x,y to to_x,to_y (or by delta px)."
    )
)
def vos_ui(
    action: str,
    x: int | None = None,
    y: int | None = None,
    clicks: int = 1,
    delta: int = 0,
    text: str | None = None,
    combo: str | None = None,
    settle_ms: int = 150,
    target: str | None = "active",
    to_x: int | None = None,
    to_y: int | None = None,
) -> dict[str, Any]:
    """Blind input: no accessibility tree exists, geometry must come from vos_status."""
    try:
        if action not in (
            "click",
            "double_click",
            "right_click",
            "middle_click",
            "wheel",
            "drag",
            "type",
            "key",
        ):
            raise ValueError(f"unknown action '{action}'")
        alive = _alive_pids()
        if not alive:
            raise RuntimeError("no live VOSviewer process")
        hwnd, info = _main_window(alive, target)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.3)
            _, info = _main_window(alive, target)
        left, top, width, height = info["rect"]
        user32.SetForegroundWindow(hwnd)
        sx = sy = None
        if action == "key":
            if not combo:
                raise ValueError("action 'key' requires 'combo', e.g. 'ctrl+s'")
            keys = [_vk(p)[0] for p in combo.split("+") if p.strip()]
            _send(
                [_key_input(k) for k in keys]
                + [_key_input(k, up=True) for k in reversed(keys)]
            )
        elif action == "type":
            if not text:
                raise ValueError("action 'type' requires 'text'")
            _send(_key_sequence(str(text)))
        else:
            sx = left + (x if x is not None else width // 2)
            sy = top + (y if y is not None else height // 2)
            if action == "wheel":
                # La rueda se despacha donde esta el puntero: primero se mueve.
                user32.SetCursorPos(sx, sy)
                time.sleep(0.05)
                _send([_mouse(0, 0, "wheel", delta & 0xFFFFFFFF)])
            else:
                if action == "drag":
                    reach = delta or 100
                    tx = left + (
                        to_x
                        if to_x is not None
                        else max(0, min(width - 4, (x or 0) + reach))
                    )
                    ty = top + (
                        to_y
                        if to_y is not None
                        else max(0, min(height - 4, (y or 0) + reach))
                    )
                    # El movimiento se hace con SetCursorPos: un MOUSEEVENTF_MOVE
                    # sin ABSOLUTE es un delta, y mandar coordenadas absolutas
                    # como delta lanzaba el puntero fuera de la pantalla.
                    user32.SetCursorPos(sx, sy)
                    _send([_mouse(0, 0, "ldown")])
                    for i in range(1, 9):
                        user32.SetCursorPos(
                            sx + (tx - sx) * i // 8, sy + (ty - sy) * i // 8
                        )
                        time.sleep(0.02)
                    _send([_mouse(0, 0, "lup")])
                else:
                    press = {
                        "click": ("ldown", "lup"),
                        "double_click": ("ldown", "lup"),
                        "right_click": ("rdown", "rup"),
                        "middle_click": ("mdown", "mup"),
                    }[action]
                    for _ in range(
                        2 if action == "double_click" else max(1, int(clicks))
                    ):
                        user32.SetCursorPos(sx, sy)
                        _send(
                            [
                                _mouse(0, 0, "move"),
                                _mouse(0, 0, press[0]),
                                _mouse(0, 0, press[1]),
                            ]
                        )
                        time.sleep(0.05)
        time.sleep(max(0, int(settle_ms)) / 1000.0)
        return {
            "ok": True,
            "action": action,
            "screen_xy": [sx, sy],
            "window_title": info["title"],
            "hwnd": hwnd,
            "window_rect": info["rect"],
            "pids": alive,
            "error_dialog": _error_dialog_text(alive),
        }
    except Exception as exc:
        return _fail(
            exc, "x/y are window-relative; get geometry from vos_status windows[]"
        )


# ------------------------------------------------------------ scopus -> VOS
#
# Measured on this machine (VOSviewer 1.6.21):
#   * `-network <file>` ALONE is accepted: VOSviewer runs layout+clustering and
#     writes a map by itself. But the map it writes carries no `label` and no
#     `description` column (header is only
#     `id x y cluster weight<Links> weight<Total link strength>`), so items are
#     labelled by bare id and the visualization is unreadable.
#   * `-map <file> -network <file>` is therefore what these runs use whenever the
#     caller owns the labels: the map supplies id/label/description plus real
#     weights, the network supplies the links, and `-run_layout -run_clustering`
#     replace the placeholder coordinates and clusters written here.
#   * For `title_terms` no map is written at all: `-corpus` + `-scores` make
#     VOSviewer do its own term extraction and its own layout.

SCOPUS_ANALYSES = ("coauthorship", "authors_sources", "title_terms")
_SCOPUS_MIN_OCCURRENCES = {"coauthorship": 2, "authors_sources": 2, "title_terms": 3}
VOSVIEWER_VERSION_NOTE = (
    "VOSviewer 1.6.21 (HISTORY.txt and Manual_VOSviewer_1.6.21.pdf ship next to "
    "VOSviewer.exe); -network alone is accepted but writes label-less maps"
)
_SCOPUS_RUNS = (
    ("01_red.png", "network_visualization"),
    ("02_overlay.png", "overlay_visualization"),
    ("03_densidad.png", "density_visualization"),
)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_authors(field: str) -> list[tuple[str, str]]:
    """'Tereci, Semih (60587916700); ...' -> [(author_id, display name), ...]."""
    out: list[tuple[str, str]] = []
    for part in (field or "").split(";"):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)\s*\((\d+)\)$", part)
        if m:
            out.append((m.group(2), m.group(1).strip()))
        else:  # no Scopus author id: fall back to the name itself as the key
            out.append((part.lower(), part))
    return out


def _scopus_rows(csv_path: str) -> list[dict[str, str]]:
    """Read a Scopus export: UTF-8 with BOM, 19 known columns, no abstract/keywords."""
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"no data rows in {csv_path}")
    required = ("Authors", "Author full names", "Title", "Source title", "Cited by")
    missing = [c for c in required if c not in rows[0]]
    if missing:
        raise ValueError(
            f"{csv_path} is not the expected Scopus 19-column export; missing {missing}"
        )
    return rows


def _scopus_documents(
    rows: list[dict[str, str]], analysis: str
) -> list[tuple[list[str], dict[str, str], float]]:
    """One (keys, key->label, citations) triple per document that yields a group."""
    docs: list[tuple[list[str], dict[str, str], float]] = []
    for row in rows:
        cited = _as_float(row.get("Cited by")) or 0.0
        keys: dict[str, str] = {}
        if analysis in ("coauthorship", "authors_sources"):
            for key, label in _split_authors(row.get("Author full names", "")):
                keys.setdefault("a|" + key, label)
        if analysis in ("authors_sources", "title_terms"):
            title = (row.get("Source title") or "").strip()
            if title:
                prefix = "s|" if analysis == "authors_sources" else ""
                keys.setdefault(prefix + title.lower(), title)
        if keys:
            docs.append((list(keys), keys, cited))
    return docs


def _cooccurrence(
    docs: list[tuple[list[str], dict[str, str], float]], min_occurrences: int
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], float]]:
    """Items weighted by document count; link weight = shared documents."""
    order: list[str] = []
    labels: dict[str, str] = {}
    counts: dict[str, int] = {}
    cites: dict[str, float] = {}
    pairs: dict[tuple[str, str], float] = {}
    for keys, label_of, cited in docs:
        for key in keys:
            if key not in labels:
                order.append(key)
                labels[key] = label_of[key]
                counts[key], cites[key] = 0, 0.0
            counts[key] += 1
            cites[key] += cited
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                pair = (a, b) if a < b else (b, a)
                pairs[pair] = pairs.get(pair, 0.0) + 1.0
    keep = [k for k in order if counts[k] >= min_occurrences]
    keepset = set(keep)
    links = {p: w for p, w in pairs.items() if p[0] in keepset and p[1] in keepset}
    degree = {k: 0 for k in keep}
    strength = {k: 0.0 for k in keep}
    for (a, b), w in links.items():
        degree[a] += 1
        degree[b] += 1
        strength[a] += w
        strength[b] += w
    items = [
        {
            "id": str(i + 1),
            "key": key,
            "label": labels[key],
            "documents": counts[key],
            "links": degree[key],
            "strength": strength[key],
            "citations": cites[key],
        }
        for i, key in enumerate(keep)
    ]
    return items, links


def _scopus_description(analysis: str, item: dict[str, Any]) -> str:
    key = item["key"]
    if key.startswith("a|") and key[2:].isdigit():
        heading, value = "Scopus author ID", key[2:]
    elif key.startswith("s|"):
        heading, value = "Source title", item["label"]
    else:
        heading, value = "Item", item["label"]
    return f"<table><tr><td>{heading}:</td><td>{value}</td></tr></table>"


def _write_scopus_map(path: str, analysis: str, items: list[dict[str, Any]]) -> None:
    """Real VOSviewer map header; x/y/cluster are placeholders that -run_layout replaces."""
    header = [
        "id",
        "label",
        "description",
        "x",
        "y",
        "cluster",
        "weight<Links>",
        "weight<Total link strength>",
        "weight<Documents>",
        "score<Citations>",
    ]
    span = max(1, len(items))
    lines = ["\t".join(header)]
    for i, it in enumerate(items):
        angle = 2 * math.pi * i / span
        lines.append(
            "\t".join(
                [
                    it["id"],
                    it["label"],
                    _scopus_description(analysis, it),
                    f"{math.cos(angle):.6f}",
                    f"{math.sin(angle):.6f}",
                    "1",
                    str(it["links"]),
                    f"{it['strength']:.6f}",
                    str(it["documents"]),
                    f"{it['citations']:.6f}",
                ]
            )
        )
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_scopus_network(
    path: str, items: list[dict[str, Any]], links: dict[tuple[str, str], float]
) -> None:
    id_of = {it["key"]: it["id"] for it in items}
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for (a, b), weight in links.items():
            fh.write(f"{id_of[a]}\t{id_of[b]}\t{weight:g}\n")


def _write_scopus_corpus(path: str, rows: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            text = re.sub(r"[^\w\s]", " ", (row.get("Title") or "").lower())
            fh.write(re.sub(r"\s+", " ", text).strip() + "\n")


def _write_scopus_scores(path: str, rows: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("score<Citations>\n")
        for row in rows:
            fh.write(f"{_as_float(row.get('Cited by')) or 0.0:g}\n")


@mcp.tool(
    description=(
        "Build a VOSviewer network straight from a Scopus CSV export (UTF-8 BOM, the "
        "19-column format: no abstract, no author/index keywords, no references) and "
        "render the three visualizations as separate batch runs. analysis='coauthorship' "
        "parses 'Author full names' into one node per Scopus author id (weight=document "
        "count, link weight=shared documents, score=Citations sum); analysis='authors_sources' "
        "builds a bipartite author-to-source network; analysis='title_terms' writes a corpus file "
        "plus a scores file and lets VOSviewer extract the terms itself. Each run writes "
        "mapa.txt/red.txt/mapa.json plus 01_red.png, 02_overlay.png, 03_densidad.png "
        "(one run per visualization, because the screenshot captures whichever "
        "visualization is selected). Returns the folder, the identifier, the per-"
        "visualization argv, item/link/cluster counts and every written file with sizes."
    )
)
def vos_from_scopus(
    csv_path: str,
    analysis: str,
    out_dir: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scopus CSV -> map files -> three batch visualization runs. No GUI is ever opened."""
    folder = ""
    try:
        if analysis not in SCOPUS_ANALYSES:
            raise ValueError(
                f"analysis must be one of {list(SCOPUS_ANALYSES)}, got {analysis!r}"
            )
        if not os.path.exists(VOSVIEWER_EXE):
            raise FileNotFoundError(f"VOSviewer not found at {VOSVIEWER_EXE}")
        csv_abs = _absolute(csv_path)
        if not os.path.exists(csv_abs):
            raise FileNotFoundError(f"csv not found: {csv_abs}")
        config = dict(params or {})
        rows = _scopus_rows(csv_abs)
        digest = _sha256_file(csv_abs)
        identifier = f"{analysis}-{digest[:8]}"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = os.path.join(_absolute(out_dir), f"{analysis}_{stamp}")
        os.makedirs(folder, exist_ok=True)

        min_occurrences = int(
            config.get("min_occurrences", _SCOPUS_MIN_OCCURRENCES[analysis])
        )
        # run_layout on bipartite author-to-source networks opens VOSviewer's
        # interactive AlgorithmProgressDialog and never returns. Keep clustering.
        layout: dict[str, Any] = {
            "run_layout": bool(config.get("run_layout", analysis != "authors_sources")),
            "run_clustering": True,
            "min_cluster_size": int(config.get("min_cluster_size", 1)),
        }
        for key in ("resolution", "attraction", "repulsion"):
            if config.get(key) is not None:
                layout[key] = config[key]

        inputs: dict[str, Any] = {}
        corpus_group: dict[str, Any] = {}
        if analysis == "title_terms":
            corpus_path = os.path.join(folder, "input_corpus.txt")
            scores_path = os.path.join(folder, "input_scores.txt")
            _write_scopus_corpus(corpus_path, rows)
            _write_scopus_scores(scores_path, rows)
            inputs = {"corpus": corpus_path, "scores": scores_path}
            corpus_group = {
                "counting_method": int(config.get("counting_method", 2)),
                "min_n_occurrences": min_occurrences,
                "n_terms": int(config.get("n_terms", 60)),
            }
        else:
            items, links = _cooccurrence(
                _scopus_documents(rows, analysis), min_occurrences
            )
            if not items or not links:
                raise ValueError(
                    f"analysis '{analysis}' produced no links with "
                    f"min_occurrences={min_occurrences} over {len(rows)} documents "
                    f"({len(items)} items survived the threshold). VOSviewer cannot "
                    "cluster or lay out a network without links, so nothing was "
                    "launched; lower min_occurrences or pick an analysis whose "
                    "documents carry more than one grouped value."
                )
            map_path = os.path.join(folder, "input_map.txt")
            network_path = os.path.join(folder, "input_network.txt")
            _write_scopus_map(map_path, analysis, items)
            _write_scopus_network(network_path, items, links)
            inputs = {"map": map_path, "network": network_path}
        if config.get("thesaurus_path"):
            inputs["thesaurus"] = _absolute(config["thesaurus_path"])

        timeout_s = float(config.get("timeout_s", 300))
        argv_per_visualization: dict[str, Any] = {}
        runs: list[dict[str, Any]] = []
        for index, (screenshot, visualization) in enumerate(_SCOPUS_RUNS):
            # Only the first run writes the map. A later run into the same paths makes
            # VOSviewer raise a modal "Replace File" dialog and hang until it is killed.
            saves: dict[str, Any] = {
                "save_screenshot_png": os.path.join(folder, screenshot),
            }
            if index == 0:
                saves.update(
                    {
                        "save_map": os.path.join(folder, "mapa.txt"),
                        "save_network": os.path.join(folder, "red.txt"),
                        "save_json": os.path.join(folder, "mapa.json"),
                    }
                )
            argv = build_argv(
                {
                    "input": dict(inputs),
                    "corpus": dict(corpus_group),
                    "save": saves,
                    "layout": dict(layout),
                    "visualization": {visualization: 1},
                },
                require_saves=True,
            )
            result = _run_batch(argv, timeout_s)
            argv_per_visualization[visualization] = argv
            written = _collect_outputs(saves)
            runs.append(
                {
                    "visualization": visualization,
                    "screenshot": screenshot,
                    "ok": bool(result.get("ok")),
                    "exit_code": result.get("exit_code"),
                    "elapsed_s": result.get("elapsed_s"),
                    "error": result.get("error"),
                    "outputs": written,
                    "missing": [f for f, v in written.items() if v.get("missing")],
                }
            )
            if not result.get("ok"):
                break

        stats = _stats_from(runs[0]["outputs"])
        failed = [r for r in runs if not r["ok"] or r["missing"]]
        generated_at = (
            datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        )
        manifest = {
            "ok": not failed and len(runs) == len(_SCOPUS_RUNS),
            "analysis": analysis,
            "source_csv": csv_abs,
            "source_csv_sha256": digest,
            "source_rows": len(rows),
            "identifier": identifier,
            "generated_at": generated_at,
            "params_used": {
                "min_occurrences": min_occurrences,
                "min_cluster_size": layout["min_cluster_size"],
                "resolution": layout.get("resolution"),
                "attraction": layout.get("attraction"),
                "repulsion": layout.get("repulsion"),
                "n_terms": corpus_group.get("n_terms"),
                "counting_method": corpus_group.get("counting_method"),
                "thesaurus_path": inputs.get("thesaurus"),
            },
            "input_mode": "corpus+scores"
            if analysis == "title_terms"
            else "map+network",
            "network_alone_note": (
                "-network alone is accepted by VOSviewer 1.6.21 but produces a "
                "label-less map; -map is passed so items keep their labels"
            ),
            "argv_per_visualization": argv_per_visualization,
            "runs": runs,
            "stats": stats,
            "vosviewer_exe": VOSVIEWER_EXE,
            "vosviewer_version_note": VOSVIEWER_VERSION_NOTE,
        }
        manifest["outputs"] = [
            {"file": name, "bytes": os.path.getsize(os.path.join(folder, name))}
            for name in sorted(os.listdir(folder))
            if os.path.isfile(os.path.join(folder, name))
        ]
        with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, default=str)

        if failed:
            failed_path = folder + "__FAILED"
            os.rename(folder, failed_path)
            manifest["folder"] = failed_path
            return {
                "ok": False,
                "error": "one or more visualization runs failed",
                "folder": failed_path,
                "identifier": identifier,
                "runs": runs,
                "manifest": manifest,
            }
        return {
            "ok": True,
            "folder": folder,
            "identifier": identifier,
            "source_csv": csv_abs,
            "source_csv_sha256": digest,
            "source_rows": len(rows),
            "generated_at": generated_at,
            "stats": stats,
            "argv_per_visualization": argv_per_visualization,
            "screenshots": [
                {
                    "file": r["screenshot"],
                    "path": r["outputs"]["save_screenshot_png"]["path"],
                    "bytes": r["outputs"]["save_screenshot_png"]["bytes"],
                }
                for r in runs
            ],
            "outputs": manifest["outputs"],
            "runs": runs,
        }
    except Exception as exc:
        return _fail(
            exc,
            "vos_from_scopus needs a Scopus 19-column CSV; it never opens the GUI",
        )


# ------------------------------------------------------------------- wiring


@mcp.tool(
    description=(
        "Return the complete VOSviewer knowledge this server holds: every accepted "
        "command-line parameter with its type and group, the enumerations, the "
        "dependency and exclusivity rules, the two execution modes, the real file "
        "formats and the measured engine facts. Call this before building argv so no "
        "parameter is guessed. Pass section to narrow to one group."
    )
)
def vos_capabilities(section: str | None = None) -> dict[str, Any]:
    """The server's own catalogue of VOSviewer 1.6.21, shaped exactly like build_argv expects."""
    try:
        by_group: dict[str, dict[str, Any]] = {}
        for flag, (kind, group) in sorted(SPECS.items()):
            by_group.setdefault(group, {})[flag] = {
                "kind": kind,
                "takes_value": kind != "flag",
                "enum": sorted(ENUM_FLAGS[flag]) if flag in ENUM_FLAGS else None,
            }
        if section is not None and section not in by_group:
            raise ValueError(
                f"section must be one of {sorted(by_group)}, got {section!r}"
            )
        return {
            "ok": True,
            "engine": "VOSviewer 1.6.21",
            "exe": VOSVIEWER_EXE,
            "exe_exists": os.path.exists(VOSVIEWER_EXE),
            "sections": sorted(by_group),
            "parameters": {section: by_group[section]} if section else by_group,
            "no_value_flags": [f for f, (k, _) in SPECS.items() if k == "flag"],
            "rules": {
                "mutually_exclusive_inputs": list(_EXCLUSIVE_INPUTS),
                "require_corpus": sorted(_NEEDS_CORPUS),
                "require_pajek_network": sorted(_NEEDS_PAJEK),
                "batch_requires_one_save": sorted(_SAVE_FLAGS + _SHOT_FLAGS),
                "interactive_forbids_save": True,
            },
            "modes": {
                "batch": (
                    "Pass at least one save_* and VOSviewer runs the job, writes every "
                    "output and exits by itself with code 0. Undocumented. Use "
                    "vos_analyze or vos_from_scopus; the server only watches for a modal "
                    "dialog and for the timeout."
                ),
                "interactive": (
                    "Pass no save_* and the window stays open for vos_open, vos_status, "
                    "vos_capture and vos_ui. The process must then be killed explicitly "
                    "through vos_close."
                ),
            },
            "process_model": {
                "launcher": "VOSviewer.exe",
                "gui_child": "javaw.exe (Java Swing; SunAwtFrame/SunAwtDialog/SunAwtToolkit)",
                "note": (
                    "VOSviewer.exe is a launcher only. The real window belongs to the "
                    "javaw.exe child, so every window lookup and every teardown walks "
                    "the process tree instead of the launched pid."
                ),
            },
            "file_formats": {
                "map": (
                    "Tab-separated, header line first: id, label, description, x, y, "
                    "cluster, then attributes whose names may carry a label between "
                    "angle brackets, e.g. weight<Total link strength>, score<Citations>."
                ),
                "network": (
                    "Tab-separated, three columns, NO header line: id1, id2, weight. An "
                    "empty or single-column file makes VOSviewer raise "
                    "'There must be at least two columns in a network file'."
                ),
                "json": "saved with a UTF-8 BOM; the root object is {network: {items, links}}.",
                "corpus": "one document per line of text; scores files must have one line per document.",
            },
            "measured_facts": [
                "Batch mode is undocumented: any save_* makes VOSviewer exit by itself with code 0.",
                "run_layout, run_clustering, skip_clustering and largest_component are bare flags; appending a value yields the modal 'Program argument <value> is invalid' and hangs the process.",
                "The overlay visualization colours items by a score attribute, not by weight; without a score<...> column the overlay renders identical to the network view.",
                "A network with zero links cannot be laid out or clustered; the server refuses before launching instead of letting VOSviewer raise a modal dialog.",
                "Swing dialogs expose no Win32 child windows, so a modal error can be detected but its message body cannot be read; only the title 'Error' is recoverable.",
                "-network alone is accepted but yields a label-less map, so -map is always passed alongside it.",
            ],
            "scopus_analyses": {
                "supported": list(SCOPUS_ANALYSES),
                "default_min_occurrences": dict(_SCOPUS_MIN_OCCURRENCES),
                "origin": "vos_from_scopus reads the 19-column Scopus export.",
                "impossible": [
                    "keyword co-occurrence: the export has no Author or Index Keywords column.",
                    "bibliographic coupling and co-citation: the export has no References column.",
                    "source co-occurrence: one Source title per document means no pairs exist.",
                ],
            },
            "dialog_policy": {
                "rules": [
                    {"title_contains": needle, "answer": choice}
                    for needle, choice in _DIALOG_RULES
                ],
                "default": _DIALOG_DEFAULT,
                "ignored_progress_titles": sorted(_PROGRESS_TITLES),
                "note": (
                    "Batch mode answers routine prompts by itself and always captures "
                    "the dialog to DEFAULT_OUT_DIR/dialogs before acting. Unknown "
                    "dialogs are captured and left unanswered rather than guessed."
                ),
                "capture_dir": _DIALOG_DIR,
            },
            "screenshot_formats": sorted(
                f.removeprefix("save_screenshot_") for f in _SHOT_FLAGS
            ),
        }
    except Exception as exc:
        return _fail(exc)


@mcp.tool(
    description=(
        "Inspect and answer the modal dialogs VOSviewer is showing right now. "
        "action='list' reports every dialog with its title, class and rect plus the "
        "answer the policy would give; action='capture' writes a PNG of the topmost "
        "dialog so the model can see it; action='yes'|'no'|'cancel' answers it. "
        "Java Swing exposes no accessibility API, so a dialog can be detected and "
        "answered by keyboard default button but its text cannot be read."
    )
)
def vos_dialog(action: str = "list", hwnd: int | None = None) -> dict[str, Any]:
    """List, capture or answer the modal dialogs of the live VOSviewer process tree."""
    try:
        if action not in ("list", "capture", "yes", "no", "cancel"):
            raise ValueError(
                f"action must be list|capture|yes|no|cancel, got {action!r}"
            )
        pids = _vosviewer_only_pids()
        if not pids:
            return {
                "ok": False,
                "error": "no live VOSviewer process tree",
                "dialogs": [],
            }
        dialogs = _modal_windows(pids)
        report = [
            {
                "hwnd": w["hwnd"],
                "title": w.get("title"),
                "class": w.get("class"),
                "rect": w.get("rect"),
                "policy_answer": _dialog_choice(w.get("title") or ""),
            }
            for w in dialogs
        ]
        if action == "list":
            return {"ok": True, "pids": pids, "count": len(report), "dialogs": report}
        if not report:
            return {"ok": False, "error": "no modal dialog is showing", "dialogs": []}
        target = next((w for w in dialogs if hwnd is None or w["hwnd"] == hwnd), None)
        if target is None:
            raise ValueError(
                f"no dialog with hwnd {hwnd}; showing {[w['hwnd'] for w in dialogs]}"
            )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shot = os.path.join(_DIALOG_DIR, f"dialog_{stamp}_{target['hwnd']}.png")
        captured, method = _capture_window_png(target["hwnd"], shot)
        if action == "capture":
            return {
                "ok": captured,
                "hwnd": target["hwnd"],
                "title": target.get("title"),
                "screenshot": shot if captured else None,
                "capture_method": method,
            }
        choice = {"yes": "yes", "no": "cancel", "cancel": "cancel"}[action]
        _send_choice(target["hwnd"], choice)
        return {
            "ok": True,
            "hwnd": target["hwnd"],
            "title": target.get("title"),
            "action": action,
            "screenshot": shot if captured else None,
            "capture_method": method,
        }
    except Exception as exc:
        return _fail(exc)


@mcp.resource(
    "vosviewer://status",
    name="VOSviewer status",
    description="Current VOSviewer process and window state as JSON.",
    mime_type="application/json",
)
def resource_status() -> str:
    return json.dumps(vos_status(), indent=2, default=str)


async def _main() -> None:
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_main())
