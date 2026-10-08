"""
otio_repair.py — OTIO Repair for Adobe Premiere Pro
Fixes .otio files exported on Mac so they import correctly on Windows Premiere 25.6+
"""

from __future__ import annotations
import json
import os
import re
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

# ─────────────────────────────────────────────
#  THEME CONSTANTS
# ─────────────────────────────────────────────
BG        = "#1e1e2e"
BG_PANEL  = "#252537"
BG_INPUT  = "#2e2e44"
FG        = "#e0e0f0"
FG_DIM    = "#888899"
ACCENT    = "#7c6af7"
RED       = "#f76a6a"
YELLOW    = "#f7c96a"
GREEN     = "#6af7a0"
FONT      = "Segoe UI"

SEVERITY_COLOR = {"error": RED, "warning": YELLOW, "info": FG_DIM}


# ─────────────────────────────────────────────
#  URL HELPERS
# ─────────────────────────────────────────────
def common_prefix(urls: list[str]) -> str:
    """Return the longest common directory prefix across a list of file:// URLs."""
    if not urls:
        return ""
    parts = []
    for url in urls:
        bare = url
        for fp in ("file:///", "file://"):
            if url.startswith(fp):
                bare = url[len(fp):]
                if not (len(bare) > 1 and bare[1] == ":") and not bare.startswith("/"):
                    bare = "/" + bare
                break
        slash = bare.rfind("/")
        parts.append(bare[: slash + 1] if slash >= 0 else "")
    result = parts[0]
    for p in parts[1:]:
        while result and not p.startswith(result):
            result = result[: result.rstrip("/").rfind("/") + 1]
    return result


def split_url(url: str) -> tuple[str, str]:
    """Return (dir_with_trailing_slash, basename) from a file:// URL."""
    bare = url
    for fp in ("file:///", "file://"):
        if url.startswith(fp):
            bare = url[len(fp):]
            if not (len(bare) > 1 and bare[1] == ":") and not bare.startswith("/"):
                bare = "/" + bare
            break
    idx = bare.rfind("/")
    if idx < 0:
        return "", bare
    return bare[: idx + 1], bare[idx + 1:]


def rebuild_url(dirpart: str, name: str) -> str:
    """Reconstruct a file:// URL from (dirpart, basename)."""
    new_bare = dirpart + name
    if new_bare.startswith("/"):
        return "file://" + new_bare
    return "file:///" + new_bare


def path_from_url(url: str) -> Optional[Path]:
    """Convert a file:// URL to a local Path, or None if not a local path."""
    if not url.startswith("file://"):
        return None
    bare = url[len("file:///"):]
    # Windows absolute path: starts with drive letter
    if len(bare) > 1 and bare[1] == ":":
        return Path(bare)
    # Unix/Mac absolute path: restore leading slash
    bare2 = url[len("file://"):]
    if bare2.startswith("/"):
        return Path(bare2)
    return None


def truncate_path(s: str, n: int = 48) -> str:
    """Shorten a path string for display."""
    if len(s) <= n:
        return s
    return "…" + s[-(n - 1):]


# ─────────────────────────────────────────────
#  ISSUE DATACLASS
# ─────────────────────────────────────────────
class Issue:
    __slots__ = ("severity", "message", "fix_key", "detail")

    def __init__(self, severity: str, message: str, fix_key: str | None = None,
                 detail: str = ""):
        self.severity = severity   # "error" | "warning" | "info"
        self.message  = message
        self.fix_key  = fix_key    # None = not auto-fixable
        self.detail   = detail


# ─────────────────────────────────────────────
#  CORE FIXER
# ─────────────────────────────────────────────
class OTIOFixer:
    def __init__(self, filepath: str):
        self.filepath  = Path(filepath)
        self.data: dict = {}
        self.issues: list[Issue] = []
        self.applied: list[str]  = []
        self.detected_path_prefix: str = ""
        self.repath_from: str = ""
        self.repath_to:   str = ""
        self.seq_width:   int | None = None
        self.seq_height:  int | None = None

    # ── Load ─────────────────────────────────
    def load(self) -> tuple[bool, str]:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            return True, ""
        except Exception as e:
            return False, str(e)

    # ── URL collection ────────────────────────
    def collect_urls(self) -> list[str]:
        """Return every unique media file:// URL found in the OTIO data."""
        seen: set[str] = set()
        results: list[str] = []

        def _walk(obj):
            if isinstance(obj, dict):
                url = obj.get("url") or obj.get("target_url")
                if isinstance(url, str) and url.startswith("file://") and url not in seen:
                    seen.add(url)
                    results.append(url)
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(self.data)
        return results

    # ── Sequence resolution ───────────────────
    def get_sequence_resolution(self) -> tuple[int | None, int | None]:
        try:
            vr = (self.data.get("tracks", {})
                           .get("metadata", {})
                           .get("PremierePro_OTIO", {})
                           .get("VideoResolution", {}))
            return vr.get("width"), vr.get("height")
        except Exception:
            return None, None

    # ── URL replacement ───────────────────────
    def update_urls(self, url_map: dict[str, str]):
        """Replace URLs in the OTIO data according to url_map {old→new}."""
        def _walk(obj):
            if isinstance(obj, dict):
                for key in ("url", "target_url"):
                    if key in obj and obj[key] in url_map:
                        obj[key] = url_map[obj[key]]
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)
        _walk(self.data)

    # ── Diagnose ─────────────────────────────
    def diagnose(self) -> list[Issue]:
        issues: list[Issue] = []

        if not self.data:
            issues.append(Issue("error", "No data loaded."))
            self.issues = issues
            return issues

        urls = self.collect_urls()

        # ── Path compatibility ────────────────
        mac_urls   = [u for u in urls if "/Volumes/" in u]
        local_urls = [u for u in urls if not u.lower().startswith("file:///volumes/")
                      and not (len(u) > 10 and u[8:10] and u[9] == ":")]

        if mac_urls:
            prefix = common_prefix(mac_urls)
            self.detected_path_prefix = prefix
            issues.append(Issue(
                "error",
                f"Media paths are Mac-format ({len(mac_urls)} file(s))",
                fix_key="repath",
                detail=(f"Common prefix: {prefix}\n"
                        "These paths won't resolve on Windows. "
                        "Use the Re-path panel to map them to your local drive."),
            ))

        # ── Crash fields ─────────────────────
        crash_found = [False]

        def _check_crash(obj):
            if isinstance(obj, dict):
                if obj.get("OTIO_SCHEMA", "").startswith("Clip"):
                    pp    = obj.get("metadata") or {}
                    inner = pp.get("PremierePro_OTIO", {}) if isinstance(pp, dict) else {}
                    if isinstance(inner, dict):
                        if "OriginalChannelGroupIndex" in inner or "SourceClipIndex" in inner:
                            crash_found[0] = True
                            return
                for v in obj.values():
                    _check_crash(v)
            elif isinstance(obj, list):
                for item in obj:
                    _check_crash(item)

        _check_crash(self.data)
        if crash_found[0]:
            issues.append(Issue(
                "error",
                "Mac-only audio metadata fields crash Premiere on Windows",
                fix_key="crash_fields",
                detail=("OriginalChannelGroupIndex and SourceClipIndex appear in audio clip "
                        "metadata. These fields are written by Premiere on Mac and cause "
                        "Premiere on Windows to crash when importing the OTIO file. "
                        "Removing them is safe — Premiere recreates them on import."),
            ))

        # ── LinearTimeWarp effects ────────────
        ltw_found = [False]

        def _check_ltw(obj):
            if isinstance(obj, dict):
                if obj.get("OTIO_SCHEMA", "").startswith("LinearTimeWarp"):
                    ltw_found[0] = True
                    return
                for v in obj.values():
                    _check_ltw(v)
            elif isinstance(obj, list):
                for item in obj:
                    _check_ltw(item)

        _check_ltw(self.data)
        if ltw_found[0]:
            issues.append(Issue(
                "warning",
                "LinearTimeWarp speed effects found (not supported by Premiere OTIO importer)",
                fix_key="linear_timewarp",
                detail=("Premiere's OTIO importer does not support LinearTimeWarp effects. "
                        "Removing them allows the file to import; clips will play at their "
                        "original speed instead."),
            ))

        # ── Global start time ─────────────────
        gst = self.data.get("global_start_time")
        if gst is not None:
            issues.append(Issue(
                "warning",
                "global_start_time is set — may shift timeline in Premiere",
                fix_key="global_start_time",
                detail=("Setting global_start_time to null aligns the timeline to zero, "
                        "which is what Premiere expects for most workflows."),
            ))

        # ── Null metadata ─────────────────────
        null_found = [False]

        def _check_null_meta(obj):
            if isinstance(obj, dict):
                meta = obj.get("metadata")
                if meta is not None and not isinstance(meta, dict):
                    null_found[0] = True
                for v in obj.values():
                    _check_null_meta(v)
            elif isinstance(obj, list):
                for item in obj:
                    _check_null_meta(item)

        _check_null_meta(self.data)
        if null_found[0]:
            issues.append(Issue(
                "warning",
                "Non-dict metadata values found",
                fix_key="null_metadata",
                detail="Some metadata fields are not dictionaries. These will be replaced with {}.",
            ))

        # ── Track kind mismatches ─────────────
        tracks = (self.data.get("tracks") or {}).get("children") or []
        kind_mismatch = False
        for track in tracks:
            if not isinstance(track, dict):
                continue
            kind = track.get("kind", "")
            name = (track.get("name") or "").lower()
            if kind == "Video" and ("audio" in name):
                kind_mismatch = True
            elif kind == "Audio" and ("video" in name):
                kind_mismatch = True
        if kind_mismatch:
            issues.append(Issue(
                "warning",
                "Track kind/name mismatch detected",
                fix_key="track_kinds",
                detail="Some tracks have mismatched kind vs. name (e.g., kind=Video but name contains 'audio').",
            ))

        # ── Rate mismatches ───────────────────
        rate_issues = [False]

        def _check_rates(obj):
            if isinstance(obj, dict):
                schema = obj.get("OTIO_SCHEMA", "")
                if schema.startswith("Clip"):
                    sr = ((obj.get("source_range") or {})
                          .get("start_time") or {})
                    mr = (((obj.get("media_reference") or {})
                           .get("available_range") or {})
                          .get("start_time") or {})
                    sr_rate = sr.get("rate")
                    mr_rate = mr.get("rate")
                    if sr_rate and mr_rate and abs(sr_rate - mr_rate) > 0.01:
                        rate_issues[0] = True
                for v in obj.values():
                    _check_rates(v)
            elif isinstance(obj, list):
                for item in obj:
                    _check_rates(item)

        _check_rates(self.data)
        if rate_issues[0]:
            issues.append(Issue(
                "info",
                "Frame rate mismatch between clip source range and media reference",
                fix_key="rate_mismatches",
                detail="Some clips have differing rates in source_range vs. media_reference.available_range. These will be normalized.",
            ))

        self.issues = issues
        return issues

    # ── Fix all ──────────────────────────────
    def fix_all(self, enabled_fixes: set | None = None) -> list[str]:
        """Apply enabled fixes. Returns list of change descriptions."""
        self.applied = []
        d = self.data

        def enabled(key: str) -> bool:
            if enabled_fixes is None:
                return True
            return key in enabled_fixes

        # Crash fields (most critical)
        if enabled("crash_fields"):
            reported = [False]
            self._fix_channel_group_meta(d, reported)

        # LinearTimeWarp
        if enabled("linear_timewarp"):
            reported = [False]
            self._fix_linear_timewarp(d, reported)

        # Global start time
        if enabled("global_start_time"):
            if d.get("global_start_time") is not None:
                d["global_start_time"] = None
                self.applied.append("Set global_start_time to null")

        # Null metadata
        if enabled("null_metadata"):
            reported = [False]
            self._fix_null_metadata(d, reported)

        # Track kinds
        if enabled("track_kinds"):
            self._fix_track_kinds(d)

        # Rate mismatches
        if enabled("rate_mismatches"):
            reported = [False]
            self._fix_rate_mismatches(d, reported)

        # Re-path media
        if self.repath_from and self.repath_to:
            reported = [False]
            self._fix_media_paths(d, reported)

        # Sequence resolution override
        if self.seq_width and self.seq_height:
            self._fix_sequence_resolution(d, self.seq_width, self.seq_height)

        return self.applied

    # ── Fix implementations ───────────────────
    def _fix_channel_group_meta(self, obj, reported):
        CRASH = ("OriginalChannelGroupIndex", "SourceClipIndex")
        if isinstance(obj, dict):
            if obj.get("OTIO_SCHEMA", "").startswith("Clip"):
                pp    = obj.get("metadata") or {}
                inner = pp.get("PremierePro_OTIO", {}) if isinstance(pp, dict) else {}
                if isinstance(inner, dict):
                    removed = [f for f in CRASH if f in inner]
                    for f in removed:
                        del inner[f]
                    if removed and not reported[0]:
                        reported[0] = True
                        self.applied.append(
                            "Removed Mac-only crash fields "
                            "(OriginalChannelGroupIndex, SourceClipIndex) from audio clips"
                        )
            for v in obj.values():
                self._fix_channel_group_meta(v, reported)
        elif isinstance(obj, list):
            for item in obj:
                self._fix_channel_group_meta(item, reported)

    def _fix_linear_timewarp(self, obj, reported):
        if isinstance(obj, dict):
            effects = obj.get("effects")
            if isinstance(effects, list):
                before = len(effects)
                obj["effects"] = [
                    e for e in effects
                    if not (isinstance(e, dict)
                            and e.get("OTIO_SCHEMA", "").startswith("LinearTimeWarp"))
                ]
                removed = before - len(obj["effects"])
                if removed and not reported[0]:
                    reported[0] = True
                    self.applied.append(
                        f"Removed {removed} LinearTimeWarp effect(s) "
                        "(not supported by Premiere OTIO importer)"
                    )
            for v in obj.values():
                self._fix_linear_timewarp(v, reported)
        elif isinstance(obj, list):
            for item in obj:
                self._fix_linear_timewarp(item, reported)

    def _fix_sequence_resolution(self, d: dict, width: int, height: int):
        try:
            pp  = (d["tracks"]
                   .setdefault("metadata", {})
                   .setdefault("PremierePro_OTIO", {}))
            old = pp.get("VideoResolution", {})
            pp["VideoResolution"] = {"width": width, "height": height}
            if old.get("width") != width or old.get("height") != height:
                self.applied.append(
                    f"Updated sequence resolution: "
                    f"{old.get('width')}×{old.get('height')} → {width}×{height}"
                )
        except Exception:
            pass

    def _fix_global_start_time(self, d: dict):
        if d.get("global_start_time") is not None:
            d["global_start_time"] = None
            self.applied.append("Set global_start_time to null")

    def _fix_null_metadata(self, obj, reported):
        if isinstance(obj, dict):
            if "metadata" in obj and not isinstance(obj["metadata"], dict):
                obj["metadata"] = {}
                if not reported[0]:
                    reported[0] = True
                    self.applied.append("Replaced non-dict metadata values with {}")
            for v in list(obj.values()):
                self._fix_null_metadata(v, reported)
        elif isinstance(obj, list):
            for item in obj:
                self._fix_null_metadata(item, reported)

    def _fix_track_kinds(self, d: dict):
        tracks = (d.get("tracks") or {}).get("children") or []
        fixed = 0
        for track in tracks:
            if not isinstance(track, dict):
                continue
            kind = track.get("kind", "")
            name = (track.get("name") or "").lower()
            if kind == "Video" and "audio" in name:
                track["kind"] = "Audio"
                fixed += 1
            elif kind == "Audio" and "video" in name:
                track["kind"] = "Video"
                fixed += 1
        if fixed:
            self.applied.append(f"Corrected {fixed} track kind mismatch(es)")

    def _fix_media_paths(self, obj, reported):
        if isinstance(obj, dict):
            for key in ("url", "target_url"):
                val = obj.get(key)
                if isinstance(val, str) and self.repath_from in val:
                    obj[key] = val.replace(self.repath_from, self.repath_to)
                    if not reported[0]:
                        reported[0] = True
                        self.applied.append(
                            f"Re-pathed media: '{self.repath_from}' → '{self.repath_to}'"
                        )
            for v in obj.values():
                self._fix_media_paths(v, reported)
        elif isinstance(obj, list):
            for item in obj:
                self._fix_media_paths(item, reported)

    def _fix_rate_mismatches(self, obj, reported):
        if isinstance(obj, dict):
            schema = obj.get("OTIO_SCHEMA", "")
            if schema.startswith("Clip"):
                sr = (obj.get("source_range") or {}).get("start_time") or {}
                mr = (((obj.get("media_reference") or {})
                       .get("available_range") or {})
                      .get("start_time") or {})
                sr_rate = sr.get("rate")
                mr_rate = mr.get("rate")
                if sr_rate and mr_rate and abs(sr_rate - mr_rate) > 0.01:
                    # Normalize media reference rate to match source range
                    ar = (obj.get("media_reference") or {}).get("available_range") or {}
                    for key in ("start_time", "duration"):
                        rt = ar.get(key)
                        if isinstance(rt, dict) and "rate" in rt:
                            value  = rt["value"] * (sr_rate / rt["rate"])
                            rt["value"] = value
                            rt["rate"]  = sr_rate
                    if not reported[0]:
                        reported[0] = True
                        self.applied.append("Normalized frame rate mismatches in clip ranges")
            for v in obj.values():
                self._fix_rate_mismatches(v, reported)
        elif isinstance(obj, list):
            for item in obj:
                self._fix_rate_mismatches(item, reported)

    # ── Save ─────────────────────────────────
    def save(self, output_path: str) -> tuple[bool, str]:
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            return True, ""
        except Exception as e:
            return False, str(e)

    def default_output_stem(self) -> str:
        return self.filepath.stem + "_fixed"

    def default_output_path(self, name: str = "") -> str:
        stem = name or self.default_output_stem()
        if not stem.endswith(".otio"):
            stem += ".otio"
        return str(self.filepath.parent / stem)

    def count_fixable(self) -> int:
        return sum(1 for i in self.issues if i.fix_key and i.fix_key != "repath")

    def count_errors(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")


# ─────────────────────────────────────────────
#  SCROLLABLE FRAME
# ─────────────────────────────────────────────
class ScrollableFrame(tk.Frame):
    def __init__(self, parent, bg=BG_PANEL, **kwargs):
        super().__init__(parent, bg=bg, **kwargs)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self._sb     = tk.Scrollbar(self, orient="vertical",
                                    command=self._canvas.yview,
                                    bg=bg, troughcolor=BG_INPUT,
                                    relief="flat", width=8)
        self._canvas.configure(yscrollcommand=self._sb.set)
        self._sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self._canvas, bg=bg)
        self._win  = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self.bind_scroll(self._canvas)
        self.bind_scroll(self.inner)

    def _on_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._win, width=event.width)

    def bind_scroll(self, widget):
        widget.bind("<MouseWheel>",
                    lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"),
                    add="+")
        widget.bind("<Button-4>",
                    lambda _: self._canvas.yview_scroll(-1, "units"), add="+")
        widget.bind("<Button-5>",
                    lambda _: self._canvas.yview_scroll(1, "units"), add="+")

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self._canvas.yview_moveto(0)


# ─────────────────────────────────────────────
#  RENAME WINDOW
# ─────────────────────────────────────────────
class RenameWindow(tk.Toplevel):
    """Modal window for renaming linked media files (on disk + in OTIO data)."""

    def __init__(self, parent: "App", fixer: OTIOFixer):
        super().__init__(parent)
        self.parent_app = parent
        self.fixer      = fixer
        self.title("Rename Linked Files")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.geometry("760x600")
        self.grab_set()

        self.unique_urls: list[str]           = fixer.collect_urls()
        self.originals:   dict[str, str]      = {}
        self.name_vars:   dict[str, tk.StringVar] = {}

        for url in self.unique_urls:
            _, basename = split_url(url)
            self.originals[url]  = basename
            self.name_vars[url]  = tk.StringVar(value=basename)

        self._build_ui()
        self._update_changed_count()

        self.update_idletasks()
        px = parent.winfo_x()
        py = parent.winfo_y()
        self.geometry(f"+{px + 40}+{py + 40}")

    def _build_ui(self):
        # Title row
        tk.Label(self, text="Rename Linked Files", font=(FONT, 16, "bold"),
                 bg=BG, fg=FG).pack(anchor="w", padx=20, pady=(16, 2))
        tk.Label(self, text="Edit names on the right. Changes rename actual files on disk "
                            "and update the OTIO references.",
                 font=(FONT, 9), bg=BG, fg=FG_DIM,
                 wraplength=720, justify="left").pack(anchor="w", padx=20, pady=(0, 8))

        # ── Batch tools ──────────────────────
        batch = tk.LabelFrame(self, text="  Batch Tools  ", font=(FONT, 9, "bold"),
                              bg=BG, fg=FG_DIM, bd=1, relief="groove")
        batch.pack(fill="x", padx=16, pady=(0, 6))
        batch_inner = tk.Frame(batch, bg=BG)
        batch_inner.pack(fill="x", padx=10, pady=6)

        # Row 1: Find / Replace
        row1 = tk.Frame(batch_inner, bg=BG)
        row1.pack(fill="x", pady=(0, 4))
        tk.Label(row1, text="Find:", font=(FONT, 9), bg=BG, fg=FG_DIM,
                 width=8, anchor="e").pack(side="left")
        self.find_var = tk.StringVar()
        tk.Entry(row1, textvariable=self.find_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=3, width=24).pack(side="left", padx=(4, 8))
        tk.Label(row1, text="Replace:", font=(FONT, 9), bg=BG, fg=FG_DIM,
                 anchor="e").pack(side="left")
        self.replace_var = tk.StringVar()
        tk.Entry(row1, textvariable=self.replace_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=3, width=24).pack(side="left", padx=(4, 8))
        tk.Button(row1, text="Replace All", font=(FONT, 9),
                  bg=BG_INPUT, fg=FG, relief="flat", padx=10, pady=3, cursor="hand2",
                  command=self._batch_find_replace).pack(side="left")

        # Row 2: Prefix / Suffix / Reset
        row2 = tk.Frame(batch_inner, bg=BG)
        row2.pack(fill="x")
        tk.Label(row2, text="Prefix:", font=(FONT, 9), bg=BG, fg=FG_DIM,
                 width=8, anchor="e").pack(side="left")
        self.prefix_var = tk.StringVar()
        tk.Entry(row2, textvariable=self.prefix_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=3, width=12).pack(side="left", padx=(4, 6))
        tk.Button(row2, text="Add Prefix", font=(FONT, 9),
                  bg=BG_INPUT, fg=FG, relief="flat", padx=8, pady=3, cursor="hand2",
                  command=self._batch_add_prefix).pack(side="left", padx=(0, 12))

        tk.Label(row2, text="Suffix:", font=(FONT, 9), bg=BG, fg=FG_DIM,
                 anchor="e").pack(side="left")
        self.suffix_var = tk.StringVar()
        tk.Entry(row2, textvariable=self.suffix_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=3, width=12).pack(side="left", padx=(4, 6))
        tk.Button(row2, text="Add Suffix", font=(FONT, 9),
                  bg=BG_INPUT, fg=FG, relief="flat", padx=8, pady=3, cursor="hand2",
                  command=self._batch_add_suffix).pack(side="left", padx=(0, 20))

        tk.Button(row2, text="Reset All", font=(FONT, 9),
                  bg=BG_INPUT, fg=FG_DIM, relief="flat", padx=8, pady=3, cursor="hand2",
                  command=self._reset_all).pack(side="left")

        # ── Changed count label ───────────────
        self.changed_lbl = tk.Label(self, text="", font=(FONT, 9), bg=BG, fg=FG_DIM)
        self.changed_lbl.pack(anchor="w", padx=20, pady=(0, 4))

        # ── Table header ─────────────────────
        hdr = tk.Frame(self, bg=BG_PANEL)
        hdr.pack(fill="x", padx=16)
        hdr.columnconfigure(0, weight=1)
        hdr.columnconfigure(1, weight=1)
        tk.Label(hdr, text="Current file", font=(FONT, 9, "bold"),
                 bg=BG_PANEL, fg=FG_DIM, anchor="w").grid(row=0, column=0, sticky="ew",
                                                            padx=8, pady=4)
        tk.Label(hdr, text="New name", font=(FONT, 9, "bold"),
                 bg=BG_PANEL, fg=FG_DIM, anchor="w").grid(row=0, column=1, sticky="ew",
                                                           padx=8, pady=4)

        # ── Scrollable table ─────────────────
        self.table_scroll = ScrollableFrame(self, bg=BG)
        self.table_scroll.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        self._build_table()

        # ── Bottom bar ───────────────────────
        bar = tk.Frame(self, bg=BG, pady=10)
        bar.pack(fill="x", padx=16)

        self.error_lbl = tk.Label(bar, text="", font=(FONT, 9), bg=BG, fg=RED,
                                  anchor="w", wraplength=500)
        self.error_lbl.pack(side="left", expand=True, fill="x")

        tk.Button(bar, text="Cancel", font=(FONT, 10),
                  bg=BG_INPUT, fg=FG_DIM, relief="flat", padx=14, pady=5, cursor="hand2",
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(bar, text="Apply Renames", font=(FONT, 10, "bold"),
                  bg=ACCENT, fg="white", activebackground="#9880ff",
                  relief="flat", padx=14, pady=5, cursor="hand2",
                  command=self._apply_renames).pack(side="right")

    def _build_table(self):
        self.table_scroll.clear()
        inner = self.table_scroll.inner
        inner.columnconfigure(0, weight=1)
        inner.columnconfigure(1, weight=1)

        for i, url in enumerate(self.unique_urls):
            dirpart, basename = split_url(url)
            row_bg = BG_PANEL if i % 2 == 0 else BG_INPUT

            # Exists indicator
            fpath  = path_from_url(url)
            exists = fpath is not None and fpath.exists()
            dot_color = GREEN if exists else FG_DIM
            dot_text  = "●" if exists else "○"

            # Left cell: indicator + current name + dir
            left = tk.Frame(inner, bg=row_bg)
            left.grid(row=i, column=0, sticky="ew", padx=(4, 2), pady=2)
            tk.Label(left, text=dot_text, font=(FONT, 9), bg=row_bg, fg=dot_color,
                     width=2).pack(side="left", anchor="n", pady=3)
            name_frame = tk.Frame(left, bg=row_bg)
            name_frame.pack(side="left", fill="x", expand=True)
            name_lbl = tk.Label(name_frame, text=basename, font=(FONT, 10),
                                bg=row_bg, fg=FG, anchor="w")
            name_lbl.pack(anchor="w")
            dir_lbl = tk.Label(name_frame,
                               text=truncate_path(dirpart),
                               font=(FONT, 8), bg=row_bg, fg=FG_DIM, anchor="w")
            dir_lbl.pack(anchor="w")
            for w in (left, name_frame, name_lbl, dir_lbl):
                self.table_scroll.bind_scroll(w)

            # Right cell: editable name entry
            right = tk.Frame(inner, bg=row_bg)
            right.grid(row=i, column=1, sticky="ew", padx=(2, 4), pady=2)
            var = self.name_vars[url]
            entry = tk.Entry(right, textvariable=var,
                             font=(FONT, 10), bg=BG_INPUT, fg=FG,
                             insertbackground=FG, relief="flat", bd=3)
            entry.pack(fill="x", expand=True, padx=4, pady=4)
            var.trace_add("write", lambda *_: self._update_changed_count())
            self.table_scroll.bind_scroll(right)

    def _update_changed_count(self):
        n = sum(
            1 for url, var in self.name_vars.items()
            if var.get().strip() != self.originals[url]
        )
        if n:
            self.changed_lbl.config(text=f"{n} file(s) will be renamed", fg=YELLOW)
        else:
            self.changed_lbl.config(text=f"{len(self.unique_urls)} file(s) — no changes",
                                    fg=FG_DIM)

    # ── Batch operations ─────────────────────
    def _batch_find_replace(self):
        find = self.find_var.get()
        if not find:
            return
        repl = self.replace_var.get()
        for var in self.name_vars.values():
            var.set(var.get().replace(find, repl))

    def _batch_add_prefix(self):
        p = self.prefix_var.get()
        if not p:
            return
        for url, var in self.name_vars.items():
            cur = var.get()
            # Don't double-prefix
            if not cur.startswith(p):
                var.set(p + cur)

    def _batch_add_suffix(self):
        s = self.suffix_var.get()
        if not s:
            return
        for url, var in self.name_vars.items():
            cur  = var.get()
            stem = Path(cur).stem
            ext  = Path(cur).suffix
            new  = stem + s + ext
            var.set(new)

    def _reset_all(self):
        for url, var in self.name_vars.items():
            var.set(self.originals[url])

    # ── Apply renames ────────────────────────
    def _apply_renames(self):
        # Build map of old→new for changed files
        to_rename: list[tuple[str, str, str]] = []  # (url, old_basename, new_basename)
        for url, var in self.name_vars.items():
            new_name = var.get().strip()
            old_name = self.originals[url]
            if new_name and new_name != old_name:
                to_rename.append((url, old_name, new_name))

        if not to_rename:
            self.destroy()
            return

        # Validate: no empty names, no path separators
        for url, old_name, new_name in to_rename:
            if not new_name:
                self.error_lbl.config(text=f"Name cannot be empty (was: {old_name})")
                return
            if "/" in new_name or "\\" in new_name:
                self.error_lbl.config(
                    text=f"Name must not contain path separators: {new_name}")
                return

        # Rename on disk + build URL map for OTIO update
        url_map: dict[str, str] = {}
        errors: list[str] = []
        renamed_count = 0

        for url, old_name, new_name in to_rename:
            dirpart, _ = split_url(url)
            new_url    = rebuild_url(dirpart, new_name)
            url_map[url] = new_url

            # Rename on disk if the file exists
            old_path = path_from_url(url)
            if old_path and old_path.exists():
                new_path = old_path.parent / new_name
                try:
                    old_path.rename(new_path)
                    renamed_count += 1
                except Exception as ex:
                    errors.append(f"{old_name}: {ex}")

        if errors:
            self.error_lbl.config(
                text="Some renames failed:\n" + "\n".join(errors[:3])
            )
            # Still update OTIO for the ones that succeeded
            succeeded = {
                old: new for (old, new) in url_map.items()
                if old not in [e.split(":")[0] for e in errors]
            }
            if succeeded:
                self.fixer.update_urls(succeeded)
        else:
            self.fixer.update_urls(url_map)

        summary = f"Renamed {renamed_count} file(s) on disk, updated OTIO references."
        if errors:
            summary += f" ({len(errors)} error(s) — see details above)"
        self.parent_app._set_status(summary, GREEN if not errors else YELLOW)
        self.destroy()


# ─────────────────────────────────────────────
#  MAIN APP
# ─────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OTIO Repair for Premiere")
        self.configure(bg=BG)

        self.fixer    : OTIOFixer | None = None
        self.fix_vars : dict[str, tk.BooleanVar] = {}
        self._build_ui()
        self.geometry("780x820")
        self.update_idletasks()
        x = (self.winfo_screenwidth()  - 780) // 2
        y = (self.winfo_screenheight() - 820) // 2
        self.geometry(f"+{x}+{y}")

    # ── Build UI ──────────────────────────────
    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=BG, pady=14)
        hdr.pack(fill="x", padx=20)
        tk.Label(hdr, text="OTIO Repair", font=(FONT, 22, "bold"),
                 bg=BG, fg=FG).pack(side="left")
        tk.Label(hdr, text="for Adobe Premiere Pro", font=(FONT, 13),
                 bg=BG, fg=FG_DIM).pack(side="left", padx=(8, 0), pady=(7, 0))

        # File open
        path_row = tk.Frame(self, bg=BG)
        path_row.pack(fill="x", padx=20, pady=(0, 4))
        tk.Button(path_row, text="Open File…", font=(FONT, 10),
                  bg=BG_INPUT, fg=FG, activebackground=ACCENT, activeforeground="white",
                  relief="flat", padx=12, pady=5, cursor="hand2",
                  command=self._browse).pack(side="left")
        self.path_var = tk.StringVar()
        tk.Label(path_row, textvariable=self.path_var,
                 font=(FONT, 10), bg=BG, fg=FG_DIM,
                 anchor="w", wraplength=520).pack(side="left", padx=(10, 0))

        # Re-path panel
        self.repath_frame = tk.LabelFrame(
            self, text="  Re-path Media  ", font=(FONT, 10, "bold"),
            bg=BG, fg=YELLOW, bd=1, relief="groove",
        )
        tk.Label(self.repath_frame,
                 text="Media paths in this file won't work on this system.\n"
                      "Enter the path prefix to replace and what to replace it with.",
                 font=(FONT, 9), bg=BG, fg=FG_DIM, justify="left",
                 wraplength=700).pack(anchor="w", padx=10, pady=(6, 4))
        rp_grid = tk.Frame(self.repath_frame, bg=BG)
        rp_grid.pack(fill="x", padx=10, pady=(0, 4))
        rp_grid.columnconfigure(1, weight=1)
        tk.Label(rp_grid, text="Replace:", font=(FONT, 10), bg=BG, fg=FG_DIM,
                 anchor="e").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        self.repath_from_var = tk.StringVar()
        tk.Entry(rp_grid, textvariable=self.repath_from_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=4).grid(row=0, column=1, sticky="ew", pady=3)
        tk.Label(rp_grid, text="With:", font=(FONT, 10), bg=BG, fg=FG_DIM,
                 anchor="e").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        self.repath_to_var = tk.StringVar()
        tk.Entry(rp_grid, textvariable=self.repath_to_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=4).grid(row=1, column=1, sticky="ew", pady=3)
        rp_btn_row = tk.Frame(self.repath_frame, bg=BG)
        rp_btn_row.pack(fill="x", padx=10, pady=(0, 8))
        tk.Button(rp_btn_row, text="Browse new location…",
                  font=(FONT, 9), bg=BG_INPUT, fg=FG,
                  relief="flat", padx=10, pady=4, cursor="hand2",
                  command=self._browse_repath).pack(side="left")
        self.repath_preview_lbl = tk.Label(rp_btn_row, text="",
                                           font=(FONT, 9), bg=BG, fg=FG_DIM)
        self.repath_preview_lbl.pack(side="left", padx=(10, 0))
        self.repath_from_var.trace_add("write", self._update_repath_preview)
        self.repath_to_var.trace_add("write",   self._update_repath_preview)
        self.repath_frame.pack_forget()

        # Sequence + output row (hidden until file loaded)
        self.meta_frame = tk.Frame(self, bg=BG)

        seq_row = tk.Frame(self.meta_frame, bg=BG)
        seq_row.pack(fill="x", pady=(4, 2))
        tk.Label(seq_row, text="Sequence size:", font=(FONT, 10),
                 bg=BG, fg=FG_DIM).pack(side="left")
        self.seq_w_var = tk.StringVar()
        self.seq_h_var = tk.StringVar()
        tk.Entry(seq_row, textvariable=self.seq_w_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=4, width=7).pack(side="left", padx=(8, 4))
        tk.Label(seq_row, text="×", font=(FONT, 10),
                 bg=BG, fg=FG_DIM).pack(side="left", padx=(0, 4))
        tk.Entry(seq_row, textvariable=self.seq_h_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=4, width=7).pack(side="left")
        tk.Label(seq_row, text="px  (applied on Fix & Save)",
                 font=(FONT, 9), bg=BG, fg=FG_DIM).pack(side="left", padx=(8, 0))

        out_row = tk.Frame(self.meta_frame, bg=BG)
        out_row.pack(fill="x", pady=(2, 4))
        tk.Label(out_row, text="Output file:", font=(FONT, 10),
                 bg=BG, fg=FG_DIM).pack(side="left")
        self.out_name_var = tk.StringVar()
        self.out_name_var.trace_add("write", lambda *_: self._update_buttons())
        tk.Entry(out_row, textvariable=self.out_name_var,
                 font=(FONT, 10), bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief="flat", bd=4, width=34).pack(side="left", padx=(8, 8))
        tk.Button(out_row, text="Browse…", font=(FONT, 9),
                  bg=BG_INPUT, fg=FG, relief="flat", padx=8, pady=3, cursor="hand2",
                  command=self._browse_output).pack(side="left")

        self.meta_frame.pack_forget()

        # Divider
        tk.Frame(self, bg=BG_INPUT, height=1).pack(fill="x", padx=20, pady=4)

        # Issues header
        issues_hdr = tk.Frame(self, bg=BG)
        issues_hdr.pack(fill="x", padx=20, pady=(4, 2))
        tk.Label(issues_hdr, text="Issues", font=(FONT, 13, "bold"),
                 bg=BG, fg=FG).pack(side="left")
        self.issues_count_lbl = tk.Label(issues_hdr, text="",
                                          font=(FONT, 9), bg=BG, fg=FG_DIM)
        self.issues_count_lbl.pack(side="left", padx=(10, 0), pady=(4, 0))

        # Select all / none
        sel_row = tk.Frame(self, bg=BG)
        sel_row.pack(fill="x", padx=20, pady=(0, 4))
        self.sel_all_lbl  = tk.Label(sel_row, text="select all", font=(FONT, 9),
                                      bg=BG, fg=ACCENT, cursor="hand2")
        self.sel_none_lbl = tk.Label(sel_row, text="select none", font=(FONT, 9),
                                      bg=BG, fg=ACCENT, cursor="hand2")
        self.sel_all_lbl.bind("<Button-1>",  lambda _: self._select_all_fixes(True))
        self.sel_none_lbl.bind("<Button-1>", lambda _: self._select_all_fixes(False))

        # Issues scrollable area
        self.issues_scroll = ScrollableFrame(self)
        self.issues_scroll.pack(fill="both", expand=True, padx=16, pady=(0, 4))

        # Bottom bar
        bar = tk.Frame(self, bg=BG, pady=10)
        bar.pack(fill="x", padx=16)
        self.status_lbl = tk.Label(bar, text="Open an .otio file to begin.",
                                    font=(FONT, 9), bg=BG, fg=FG_DIM,
                                    anchor="w", wraplength=440)
        self.status_lbl.pack(side="left", expand=True, fill="x")

        self.rename_btn = tk.Button(bar, text="Rename Files…", font=(FONT, 10),
                                     bg=BG_INPUT, fg=FG, relief="flat",
                                     padx=12, pady=5, cursor="hand2",
                                     state="disabled",
                                     command=self._open_rename_window)
        self.rename_btn.pack(side="right", padx=(6, 0))

        self.saveas_btn = tk.Button(bar, text="Fix & Save As…", font=(FONT, 10),
                                     bg=BG_INPUT, fg=FG, relief="flat",
                                     padx=12, pady=5, cursor="hand2",
                                     state="disabled",
                                     command=self._fix_and_save_as)
        self.saveas_btn.pack(side="right", padx=(6, 0))

        self.fix_btn = tk.Button(bar, text="Fix & Save", font=(FONT, 10, "bold"),
                                  bg=ACCENT, fg="white", activebackground="#9880ff",
                                  relief="flat", padx=16, pady=5, cursor="hand2",
                                  state="disabled",
                                  command=self._fix_and_save)
        self.fix_btn.pack(side="right", padx=(6, 0))

    # ── Issues rendering ──────────────────────
    def _render_issues(self, issues: list[Issue], applied: list[str] | None = None,
                       out_path: str = ""):
        self.issues_scroll.clear()
        inner = self.issues_scroll.inner
        pad = 8

        if applied:
            applied_frame = tk.Frame(inner, bg=BG_PANEL, pady=4)
            applied_frame.pack(fill="x", padx=pad, pady=(6, 4))
            tk.Label(applied_frame, text="Applied fixes:", font=(FONT, 9, "bold"),
                     bg=BG_PANEL, fg=GREEN, anchor="w").pack(fill="x", padx=8)
            for msg in applied:
                tk.Label(applied_frame, text=f"  ✓ {msg}", font=(FONT, 9),
                         bg=BG_PANEL, fg=GREEN, anchor="w",
                         justify="left", wraplength=680).pack(fill="x", padx=8, pady=1)
            if out_path:
                tk.Label(applied_frame, text=f"  Saved: {out_path}",
                         font=(FONT, 9), bg=BG_PANEL, fg=FG_DIM,
                         anchor="w", wraplength=680).pack(fill="x", padx=8, pady=(4, 2))

        if not issues:
            tk.Label(inner, text="No issues found — file looks good!",
                     font=(FONT, 11), bg=BG_PANEL, fg=GREEN,
                     pady=20).pack(fill="x", padx=pad)
            self.issues_count_lbl.config(text="0 issues", fg=GREEN)
            # Hide select all/none
            self.sel_all_lbl.pack_forget()
            self.sel_none_lbl.pack_forget()
            return

        # Show select all/none if there are checkable issues
        fixable = sum(1 for i in issues if i.fix_key and i.fix_key != "repath")
        if fixable:
            self.sel_all_lbl.pack(side="left")
            self.sel_none_lbl.pack(side="left", padx=(8, 0))
        else:
            self.sel_all_lbl.pack_forget()
            self.sel_none_lbl.pack_forget()

        for group_label, severity in [("Errors", "error"), ("Warnings", "warning"), ("Info", "info")]:
            group = [i for i in issues if i.severity == severity]
            if not group:
                continue
            tk.Label(inner, text=group_label, font=(FONT, 10, "bold"),
                     bg=BG_PANEL, fg=FG, anchor="w").pack(fill="x", padx=pad, pady=(10, 2))
            for issue in group:
                self._add_issue_row(inner, issue, pad)

        fixable = sum(1 for i in issues if i.fix_key and i.fix_key != "repath")
        if fixable:
            tk.Label(inner, text="Checked items will be fixed when you click Fix & Save.",
                     font=(FONT, 9), bg=BG_PANEL, fg=FG_DIM,
                     anchor="w").pack(fill="x", padx=pad, pady=(10, 6))

        n = len(issues)
        errors = sum(1 for i in issues if i.severity == "error")
        self.issues_count_lbl.config(
            text=f"{n} issue{'s' if n != 1 else ''}  ({fixable} fixable)",
            fg=RED if errors else YELLOW,
        )

    def _add_issue_row(self, parent, issue: Issue, pad: int):
        color = SEVERITY_COLOR.get(issue.severity, FG)
        is_checkable = issue.fix_key and issue.fix_key != "repath"

        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill="x", padx=pad, pady=(3, 0))
        top = tk.Frame(row, bg=BG_PANEL)
        top.pack(fill="x")

        if is_checkable:
            var = self.fix_vars.setdefault(issue.fix_key, tk.BooleanVar(value=True))
            cb = tk.Checkbutton(top, variable=var,
                                bg=BG_PANEL, activebackground=BG_PANEL,
                                selectcolor=BG_INPUT, fg=color,
                                relief="flat", bd=0, cursor="hand2",
                                command=self._update_buttons)
            cb.pack(side="left", padx=(0, 4))
            self.issues_scroll.bind_scroll(cb)
        else:
            tk.Label(top, text="●" if issue.severity == "error" else "○",
                     font=(FONT, 10), bg=BG_PANEL, fg=color,
                     width=2, anchor="e").pack(side="left", padx=(0, 4))

        msg_lbl = tk.Label(top, text=issue.message, font=(FONT, 10),
                           bg=BG_PANEL, fg=color, anchor="w", justify="left", wraplength=680)
        msg_lbl.pack(side="left", fill="x", expand=True)
        self.issues_scroll.bind_scroll(msg_lbl)

        if issue.detail:
            det = tk.Label(row, text=issue.detail, font=(FONT, 9),
                           bg=BG_PANEL, fg=FG_DIM, anchor="w",
                           justify="left", wraplength=680, padx=22)
            det.pack(fill="x", pady=(1, 4))
            self.issues_scroll.bind_scroll(det)

    # ── File actions ──────────────────────────
    def _browse(self):
        path = filedialog.askopenfilename(
            title="Open OTIO File",
            filetypes=[("OpenTimelineIO", "*.otio"), ("All Files", "*.*")],
        )
        if path:
            self._load_file(path)

    def _browse_repath(self):
        folder = filedialog.askdirectory(title="Select New Media Root Folder")
        if folder:
            self.repath_to_var.set(folder.replace("\\", "/"))

    def _browse_output(self):
        if not self.fixer:
            return
        cur = self.out_name_var.get() or self.fixer.default_output_stem() + ".otio"
        out = filedialog.asksaveasfilename(
            title="Save Fixed OTIO As",
            defaultextension=".otio",
            initialdir=str(self.fixer.filepath.parent),
            initialfile=cur,
            filetypes=[("OpenTimelineIO", "*.otio"), ("All Files", "*.*")],
        )
        if out:
            self.out_name_var.set(Path(out).name)

    def _load_file(self, path: str):
        self.fixer = OTIOFixer(path)
        ok, err = self.fixer.load()
        if not ok:
            self._set_status(f"Load error: {err}", RED)
            messagebox.showerror("Load Error", err)
            return

        self.path_var.set(path)
        issues = self.fixer.diagnose()

        # Populate sequence size from file
        w, h = self.fixer.get_sequence_resolution()
        self.seq_w_var.set(str(w) if w else "")
        self.seq_h_var.set(str(h) if h else "")

        # Default output name
        self.out_name_var.set(self.fixer.default_output_stem() + ".otio")

        self._maybe_show_repath()
        self._render_issues(issues)
        self.meta_frame.pack(fill="x", padx=20, pady=(4, 0))
        self.rename_btn.config(state="normal" if self.fixer.collect_urls() else "disabled")
        self._update_buttons()

    def _maybe_show_repath(self):
        if not self.fixer:
            return
        urls = self.fixer.collect_urls()
        mac_urls = [u for u in urls if "/Volumes/" in u]
        if mac_urls:
            prefix = common_prefix(mac_urls)
            self.repath_from_var.set(prefix.replace("file:///", "").replace("file://", ""))
            self.repath_frame.pack(fill="x", padx=16, pady=(0, 4))
        else:
            self.repath_frame.pack_forget()

    def _update_repath_preview(self, *_):
        frm = self.repath_from_var.get().strip()
        to  = self.repath_to_var.get().strip()
        if frm and to and self.fixer:
            urls = self.fixer.collect_urls()
            sample = next((u for u in urls if frm in u), None)
            if sample:
                preview = sample.replace(frm, to)
                self.repath_preview_lbl.config(
                    text=f"e.g. {truncate_path(preview, 60)}", fg=FG_DIM)
                return
        self.repath_preview_lbl.config(text="")

    # ── Select all/none ────────────────────────
    def _select_all_fixes(self, value: bool):
        for var in self.fix_vars.values():
            var.set(value)
        self._update_buttons()

    # ── Open rename window ────────────────────
    def _open_rename_window(self):
        if self.fixer:
            RenameWindow(self, self.fixer)

    # ── Fix & Save ────────────────────────────
    def _fix_and_save(self):
        if not self.fixer:
            return
        out = self.fixer.default_output_path(self.out_name_var.get().strip())
        self._run_fix(out)

    def _fix_and_save_as(self):
        if not self.fixer:
            return
        cur = self.out_name_var.get() or self.fixer.default_output_stem() + ".otio"
        out = filedialog.asksaveasfilename(
            title="Save Fixed OTIO As",
            defaultextension=".otio",
            initialdir=str(self.fixer.filepath.parent),
            initialfile=cur,
            filetypes=[("OpenTimelineIO", "*.otio"), ("All Files", "*.*")],
        )
        if out:
            self.out_name_var.set(Path(out).name)
            self._run_fix(out)

    def _run_fix(self, out_path: str):
        self.fixer.repath_from = self.repath_from_var.get().replace("\\", "/")
        self.fixer.repath_to   = self.repath_to_var.get().replace("\\", "/")

        # Sequence size override
        try:
            w = int(self.seq_w_var.get().strip())
            h = int(self.seq_h_var.get().strip())
            self.fixer.seq_width  = w if w > 0 else None
            self.fixer.seq_height = h if h > 0 else None
        except (ValueError, AttributeError):
            self.fixer.seq_width  = None
            self.fixer.seq_height = None

        enabled = {k for k, v in self.fix_vars.items() if v.get()}
        changes = self.fixer.fix_all(enabled_fixes=enabled)

        ok, err = self.fixer.save(out_path)
        if not ok:
            self._set_status(f"Save error: {err}", RED)
            messagebox.showerror("Save Error", err)
            return

        remaining = self.fixer.diagnose()
        self._maybe_show_repath()
        self._render_issues(remaining, applied=changes, out_path=out_path)
        self._set_status(f"Saved → {Path(out_path).name}", GREEN)
        self._update_buttons()

    # ── Buttons & status ─────────────────────
    def _update_buttons(self):
        if not self.fixer:
            self.fix_btn.config(state="disabled")
            self.saveas_btn.config(state="disabled")
            return

        any_checked  = any(v.get() for v in self.fix_vars.values())
        has_repath   = bool(self.repath_from_var.get() and self.repath_to_var.get())
        has_out_name = bool(self.out_name_var.get().strip())
        can_fix      = (any_checked or has_repath) and has_out_name

        self.fix_btn.config(state="normal" if can_fix else "disabled")
        self.saveas_btn.config(state="normal" if (any_checked or has_repath) else "disabled")

        repath_needed = any(i.fix_key == "repath" for i in self.fixer.issues)

        if repath_needed and not has_repath and not any_checked:
            self._set_status("Fill in Re-path Media above, then click Fix & Save.", YELLOW)
        elif not any_checked and not has_repath:
            self._set_status("Check fixes above to apply, then click Fix & Save.", FG_DIM)
        elif any_checked or has_repath:
            self._set_status("Ready to fix. Click Fix & Save.", ACCENT)
        else:
            self._set_status("Nothing selected.", FG_DIM)

    def _set_status(self, msg: str, color: str = FG_DIM):
        self.status_lbl.config(text=msg, fg=color)


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    app = App()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        app.after(100, lambda: app._load_file(sys.argv[1]))
    app.mainloop()
