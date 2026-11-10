#!/usr/bin/env python3
"""Desktop window for the SimScale solid temperature extractor (Tkinter, runs on Windows, macOS and Linux).

Deliberately minimal: one link, one output folder, one button. Runs the extractor as a child process (not in-process)
so Cancel can stop it immediately and cleanly, and the API key travels via an environment variable rather than a
visible command-line argument."""
import os, queue, subprocess, sys, threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import simscale_solid_temperature_extractor as core

APP_TITLE = "SimScale Solid Temperature Extractor"


def resource_dir():
    """Folder with bundled files: PyInstaller unpack folder, else the folder of this script."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def cli_command():
    """-> argv prefix that runs the command-line tool: the sibling .exe when frozen, else `python3 script.py`."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        name = "simscale_solid_temperature_extractor_cli.exe" if sys.platform.startswith("win") \
            else "simscale_solid_temperature_extractor_cli"
        cli = exe_dir / name
        if not cli.exists():
            raise FileNotFoundError(f"{name} not found next to {sys.executable}")
        return [str(cli)]
    return [sys.executable, str(resource_dir() / "simscale_solid_temperature_extractor.py")]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x580")
        self.minsize(640, 480)
        self.q = queue.Queue()
        self.reader = None
        self.proc = None
        self.api_key = tk.StringVar(value=os.environ.get("SIMSCALE_API_KEY", ""))
        self.link = tk.StringVar()
        self.zip_path = tk.StringVar()
        self.out = tk.StringVar(value=str(Path.home() / "Desktop" / "SimScale_Solid_Temperatures"))
        self.write_htc = tk.BooleanVar(value=True)
        self._build()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 10, "pady": 6}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=10, pady=10)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="1. SimScale API key", font=("", 10, "bold")).grid(row=0, column=0, sticky="w", **pad)
        key_row = ttk.Frame(frm)
        key_row.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        key_row.columnconfigure(0, weight=1)
        self.key_entry = ttk.Entry(key_row, textvariable=self.api_key, show="•")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_row, text="Show", variable=self.show_key, command=self._toggle_key).grid(
            row=0, column=1, padx=(8, 0))
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

        ttk.Checkbutton(
            frm, text="Also write a convective (HTC) table for thermomechanical", variable=self.write_htc
        ).grid(row=6, column=1, columnspan=2, sticky="w", padx=10)
        ttk.Label(
            frm, text="Same positions as the temperature table, HTC = 1e9 W/(m²K) - thermomechanical only "
                      "accepts whole-body temperatures, so this pair forces the wall temperature to follow the "
                      "table via a convective boundary condition instead.",
            foreground="#555555", wraplength=620, justify="left"
        ).grid(row=7, column=1, columnspan=2, sticky="w", padx=10)

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(14, 6))
        self.btn_run = ttk.Button(btns, text="Extract temperatures", command=self._start)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(btns, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)
        self.btn_open = ttk.Button(btns, text="Open output folder", command=self._open_out)
        self.btn_open.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(btns, mode="indeterminate", length=160)
        self.progress.pack(side="left", padx=8)

        self.log = ScrolledText(frm, height=16, state="disabled", font=("Courier", 10))
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=10, pady=6)
        frm.rowconfigure(9, weight=1)

    # ------------------------------------------------------------------ actions
    def _toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "•")

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

    def _build_command(self):
        """-> (argv, env) for the child process. The API key travels via env, never on the command line."""
        link, zpath = self.link.get().strip(), self.zip_path.get().strip()
        if not link and not zpath:
            raise ValueError("Paste a SimScale link, or choose a result zip.")
        if link and zpath:
            raise ValueError("Use either a link or a result zip, not both.")
        env = dict(os.environ)
        argv = cli_command()
        if zpath:
            if not Path(zpath).exists():
                raise ValueError(f"Zip file not found: {zpath}")
            argv += ["--zip", zpath]
        else:
            key = self.api_key.get().strip()
            if not key:
                raise ValueError("Enter the SimScale API key.")
            argv += ["--run", link]
            env["SIMSCALE_API_KEY"] = key
        argv += ["--out", self.out.get()]
        if not self.write_htc.get():
            argv += ["--no-htc-table"]
        return argv, env

    def _start(self):
        if self.proc is not None:
            return
        try:
            argv, env = self._build_command()
        except (ValueError, FileNotFoundError) as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self._clear()
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.start(12)
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        try:
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                env=env, creationflags=creationflags)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Could not start the extractor: {e}")
            self._reset_buttons()
            return
        self.reader = threading.Thread(target=self._read_proc, daemon=True)
        self.reader.start()

    def _cancel(self):
        if self.proc is not None:
            self.q.put("\n--- cancelling ---\n")
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.btn_cancel.configure(state="disabled")

    def _read_proc(self):
        proc = self.proc
        try:
            for line in proc.stdout:
                self.q.put(line)
        finally:
            proc.stdout.close()
            rc = proc.wait()
            self.q.put(("__done__", rc == 0, rc))

    def _on_close(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.destroy()

    def _reset_buttons(self):
        self.proc = None
        self.progress.stop()
        self.btn_run.configure(state="normal")
        self.btn_cancel.configure(state="disabled")

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
                    _, ok, rc = item
                    if ok:
                        msg = "Finished."
                    elif rc < 0:
                        msg = "Cancelled."
                    else:
                        msg = "Stopped with an error (see above)."
                    self._append("\n" + msg + "\n")
                    self._reset_buttons()
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
