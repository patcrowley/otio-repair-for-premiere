# OTIO Repair for Adobe Premiere Pro

Fixes `.otio` timeline files exported on Mac so they import correctly into **Adobe Premiere Pro 25.6+** on Windows.

## The Problem

Premiere on Mac writes two metadata fields (`OriginalChannelGroupIndex`, `SourceClipIndex`) into audio clip metadata that crash Premiere's Windows OTIO importer. Mac media paths (`file:///Volumes/…`) also need remapping to Windows paths.

## Two Ways to Use It

### Web app (universal — no install)
Open **`otio_repair.html`** in any browser. Drag & drop your `.otio` file, check the fixes you want, click **Fix & Download**. Works on Mac, Windows, and Linux.

### Desktop app (Windows)
Run `python otio_repair.py` (requires Python 3.9+).  
Or double-click **`Build Standalone EXE.bat`** to create a self-contained `.exe` with PyInstaller.

## What It Fixes

| Issue | Severity | Fix key |
|---|---|---|
| Mac-only crash fields (`OriginalChannelGroupIndex`, `SourceClipIndex`) | 🔴 Error | `crash_fields` |
| Mac media paths (`/Volumes/…`) | 🔴 Error | `repath` |
| `LinearTimeWarp` speed effects (not supported by importer) | 🟡 Warning | `linear_timewarp` |
| `global_start_time` offset | 🟡 Warning | `global_start_time` |
| Non-object metadata values | 🟡 Warning | `null_metadata` |

## Features

- **Selective fixes** — checkbox per issue; only apply what you want
- **Re-path media** — remap Mac path prefix to your local Windows drive
- **Rename linked files** — edit file names in a table; OTIO references update automatically
- **Sequence size override** — set Width × Height applied on save
- **Editable output filename** — defaults to `originalname_fixed.otio`

## Requirements

- Python 3.9+ (desktop app only)
- Any modern browser (web app)
- Adobe Premiere Pro 25.6 or later
