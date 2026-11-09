"""Open the operating system's folder dialog and return the chosen folder.

The browser cannot hand a page the absolute path of a folder (``<input
webkitdirectory>`` gives file names only), so Home > "Change folder" asks the
server, which runs on the same computer, to show the native dialog:

* Windows: the Explorer folder picker through PowerShell (FolderBrowserDialog);
* macOS:   ``osascript`` "choose folder" (Finder's dialog);
* Linux:   ``zenity``, else ``kdialog``, else tkinter's dialog.

Returns the path, ``""`` when the user cancelled, or raises ``PickerUnavailable``
when no dialog can be shown (a headless machine). Nothing is changed here: the
route passes the path to ``PermissionPolicy.update`` so the usual workspace
validation applies.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT = 600  # seconds the dialog may stay open


class PickerUnavailable(RuntimeError):
    pass


def _run(cmd: list[str]) -> tuple[int, str]:
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)  # noqa: S603 - fixed argv
    return done.returncode, (done.stdout or "").strip()


def pick_folder(start: str | None = None, title: str = "Choose the folder IMURUN works in") -> str:
    start = str(Path(os.path.expanduser(start or "~")))
    if sys.platform.startswith("win"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            f"$d.Description = '{title}'; $d.ShowNewFolderButton = $true;"
            f"$d.SelectedPath = '{start.replace(chr(39), chr(39) * 2)}';"
            "$top = New-Object System.Windows.Forms.Form; $top.TopMost = $true;"
            "if ($d.ShowDialog($top) -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }"
        )
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            raise PickerUnavailable("PowerShell is not available to show the folder dialog")
        _, out = _run([exe, "-NoProfile", "-STA", "-Command", script])
        return out
    if sys.platform == "darwin":
        script = (f'POSIX path of (choose folder with prompt "{title}" '
                  f'default location (POSIX file "{start}"))')
        code, out = _run(["osascript", "-e", script])
        return out.rstrip("/") if code == 0 else ""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise PickerUnavailable("no desktop session to show a folder dialog in")
    if shutil.which("zenity"):
        code, out = _run(["zenity", "--file-selection", "--directory", f"--title={title}",
                          f"--filename={start.rstrip('/')}/"])
        return out if code == 0 else ""
    if shutil.which("kdialog"):
        code, out = _run(["kdialog", "--getexistingdirectory", start, "--title", title])
        return out if code == 0 else ""
    try:
        code, out = _run([sys.executable, "-c",
                          "import sys, tkinter as tk; from tkinter import filedialog; r = tk.Tk(); "
                          "r.withdraw(); r.attributes('-topmost', True); "
                          "p = filedialog.askdirectory(initialdir=sys.argv[1], title=sys.argv[2]); "
                          "sys.stdout.write(p or '')", start, title])
    except (OSError, subprocess.SubprocessError) as exc:
        raise PickerUnavailable(f"no folder dialog is available ({type(exc).__name__})") from exc
    if code != 0:
        raise PickerUnavailable("no folder dialog is available (install zenity)")
    return out
