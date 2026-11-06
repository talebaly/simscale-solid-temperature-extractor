#!/usr/bin/env python3
"""Desktop window for the SimScale solid temperature extractor (Tkinter, runs on Windows, macOS and Linux).

Deliberately minimal: one link, one output folder, one button."""
import os, queue, subprocess, sys, threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import simscale_solid_temperature_extractor as core

APP_TITLE = "SimScale Solid Temperature Extractor"


class QueueWriter:
    """File-like object that forwards text written by the worker thread to the GUI."""

    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x560")
        self.minsize(640, 460)
        self.q = queue.Queue()
        self.worker = None
        self.api_key = tk.StringVar(value=os.environ.get("SIMSCALE_API_KEY", ""))
        self.link = tk.StringVar()
        self.zip_path = tk.StringVar()
        self.out = tk.StringVar(value=str(Path.home() / "Desktop" / "SimScale_Solid_Temperatures"))
        self._build()
        self.after(100, self._drain)

    # ------------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 10, "pady": 6}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=10, pady=10)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="1. SimScale API key", font=("", 10, "bold")).grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.api_key, show="•").grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(frm, text="Avatar (top right) → User Settings → API Keys → Generate Key.",
                  foreground="#555555").grid(row=1, column=1, columnspan=2, sticky="w", padx=10)

        ttk.Label(frm, text="2. Finished CHT run", font=("", 10, "bold")).grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.link).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Use result zip instead…", command=self._pick_zip).grid(row=2, column=2, **pad)
        self.zip_label = ttk.Label(frm, textvariable=self.zip_path, foreground="#555555")
        self.zip_label.grid(row=3, column=1, columnspan=2, sticky="w", padx=10)
        ttk.Label(frm, text="Paste the SimScale Workbench link of the finished run (or its simulation).",
                  foreground="#555555").grid(row=4, column=1, columnspan=2, sticky="w", padx=10)

        ttk.Label(frm, text="3. Output folder", font=("", 10, "bold")).grid(row=5, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.out).grid(row=5, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…", command=self._browse_out).grid(row=5, column=2, **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(14, 6))
        self.btn_run = ttk.Button(btns, text="Extract temperatures", command=self._start)
        self.btn_run.pack(side="left")
        self.btn_open = ttk.Button(btns, text="Open output folder", command=self._open_out)
        self.btn_open.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(btns, mode="indeterminate", length=180)
        self.progress.pack(side="left", padx=8)

        self.log = ScrolledText(frm, height=16, state="disabled", font=("Courier", 10))
        self.log.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=10, pady=6)
        frm.rowconfigure(7, weight=1)

    # ------------------------------------------------------------------ actions
    def _pick_zip(self):
        p = filedialog.askopenfilename(title="SimScale result archive", filetypes=[("Zip archive", "*.zip")])
        if p:
            self.zip_path.set(p)
            self.link.set("")

    def _browse_out(self):
        p = filedialog.askdirectory(title="Output folder")
        if p:
            self.out.set(p)

    def _open_out(self):
        p = Path(self.out.get())
        if not p.exists():
            messagebox.showinfo(APP_TITLE, "The output folder does not exist yet. Run the extraction first.")
            return
        open_with_system(p)

    def _build_argv(self):
        link, zpath = self.link.get().strip(), self.zip_path.get().strip()
        if not link and not zpath:
            raise ValueError("Paste a SimScale link, or choose a result zip.")
        if link and zpath:
            raise ValueError("Use either a link or a result zip, not both.")
        argv = []
        if zpath:
            if not Path(zpath).exists():
                raise ValueError(f"Zip file not found: {zpath}")
            argv += ["--zip", zpath]
        else:
            if not self.api_key.get().strip():
                raise ValueError("Enter the SimScale API key.")
            argv += ["--run", link, "--api-key", self.api_key.get().strip()]
        argv += ["--out", self.out.get()]
        return argv

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            argv = self._build_argv()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self._clear()
        self.btn_run.configure(state="disabled")
        self.progress.start(12)
        self.worker = threading.Thread(target=self._work, args=(argv,), daemon=True)
        self.worker.start()

    def _work(self, argv):
        w = QueueWriter(self.q)
        old = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = w
        ok = False
        try:
            core.main(argv)
            ok = True
        except SystemExit as e:
            if e.code not in (None, 0):
                w.write(f"\nERROR: {e.code}\n")
            else:
                ok = True
        except Exception as e:  # report anything unexpected in the log instead of crashing the window
            w.write(f"\nERROR: {type(e).__name__}: {e}\n")
        finally:
            sys.stdout, sys.stderr = old
            self.q.put(("__done__", ok))

    # ------------------------------------------------------------------ log handling
    def _clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _append(self, text):
        self.log.configure(state="normal")
        parts = text.split("\r")
        for i, part in enumerate(parts):
            if i > 0:  # carriage return: overwrite the current line (download progress)
                self.log.delete("end-1c linestart", "end-1c")
            self.log.insert("end", part)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__done__":
                    self.progress.stop()
                    self.btn_run.configure(state="normal")
                    self._append("\n" + ("Finished." if item[1] else "Stopped with an error (see above).") + "\n")
                else:
                    self._append(item)
        except queue.Empty:
            pass
        self.after(100, self._drain)


def open_with_system(path):
    """Open a file or folder with the default program of the operating system."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 (Windows only)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def main():
    if "--selftest" in sys.argv:  # used by the Windows build check (no window)
        core.selftest()
        return
    App().mainloop()


if __name__ == "__main__":
    main()
