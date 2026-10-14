#!/usr/bin/env python3
"""Doppelte Dateien finden – eine grafische Linux-Anwendung (Tkinter).

Sucht in einem gewählten Verzeichnis (rekursiv) nach Dateien mit identischem
Inhalt, zeigt sie gruppiert mit Dateigröße an und erlaubt es, Dateien zu
markieren und zu löschen (wahlweise in den Papierkorb).
"""

import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

CHUNK_SIZE = 1024 * 1024
PARTIAL_SIZE = 64 * 1024
UNCHECKED = "☐"
CHECKED = "☑"


def human_size(num_bytes):
    """Bytes in eine lesbare Größe umwandeln (z. B. 1,5 MB)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}".replace(".", ",")
        size /= 1024
    return f"{num_bytes} B"


def file_hash(path, partial=False):
    """Hash über den Dateiinhalt (bei partial=True nur der Anfang)."""
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        if partial:
            h.update(f.read(PARTIAL_SIZE))
        else:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                h.update(chunk)
    return h.hexdigest()


def find_duplicates(root, min_size=1, include_hidden=False, progress=None, cancel=None):
    """Liefert eine Liste von Gruppen (Listen von Pfaden) mit identischem Inhalt.

    Vorgehen: erst nach Größe gruppieren, dann nach Hash des Dateianfangs,
    zuletzt nach Hash des vollständigen Inhalts.
    """
    progress = progress or (lambda msg, frac=None: None)
    cancel = cancel or threading.Event()

    by_size = defaultdict(list)
    seen_inodes = set()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if cancel.is_set():
            return []
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            filenames = [f for f in filenames if not f.startswith(".")]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            # Hardlinks auf dieselbe Datei nur einmal zählen
            key = (st.st_dev, st.st_ino)
            if key in seen_inodes:
                continue
            seen_inodes.add(key)
            if st.st_size < min_size:
                continue
            by_size[st.st_size].append(path)
            count += 1
            if count % 500 == 0:
                progress(f"Durchsuche Verzeichnisse … {count} Dateien gefunden")

    candidates = [paths for paths in by_size.values() if len(paths) > 1]
    total = sum(len(p) for p in candidates)
    done = 0
    groups = []
    for paths in candidates:
        by_partial = defaultdict(list)
        for path in paths:
            if cancel.is_set():
                return []
            try:
                by_partial[file_hash(path, partial=True)].append(path)
            except OSError:
                pass
            done += 1
            progress(f"Vergleiche Inhalte … {done}/{total}", done / max(total, 1))
        for same_start in by_partial.values():
            if len(same_start) < 2:
                continue
            by_full = defaultdict(list)
            for path in same_start:
                if cancel.is_set():
                    return []
                try:
                    by_full[file_hash(path)].append(path)
                except OSError:
                    pass
            groups.extend(sorted(g) for g in by_full.values() if len(g) > 1)

    groups.sort(key=lambda g: os.path.getsize(g[0]) * (len(g) - 1), reverse=True)
    return groups


def move_to_trash(path):
    """Datei in den Papierkorb verschieben (gio oder send2trash)."""
    if shutil.which("gio"):
        subprocess.run(["gio", "trash", "--", path], check=True, capture_output=True)
        return
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise OSError("Kein Papierkorb verfügbar (weder 'gio' noch 'send2trash')") from exc
    send2trash(path)


class DuplicateFinderApp:
    def __init__(self, master):
        self.master = master
        master.title("Doppelte Dateien finden")
        master.geometry("1000x600")
        master.minsize(700, 400)

        self.directory = tk.StringVar(value=os.path.expanduser("~"))
        self.min_size_kb = tk.StringVar(value="0")
        self.include_hidden = tk.BooleanVar(value=False)
        self.use_trash = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Verzeichnis wählen und „Suchen“ klicken.")

        self.checked = set()        # Treeview-IDs markierter Dateien
        self.item_info = {}         # Treeview-ID -> (Pfad, Größe)
        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None

        self._build_ui()
        self.master.after(100, self._poll_queue)

    # ---------- Oberfläche ----------
    def _build_ui(self):
        top = ttk.Frame(self.master, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Verzeichnis:").pack(side="left")
        ttk.Entry(top, textvariable=self.directory).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Durchsuchen …", command=self.choose_directory).pack(side="left")
        self.scan_button = ttk.Button(top, text="Suchen", command=self.start_scan)
        self.scan_button.pack(side="left", padx=(4, 0))

        opts = ttk.Frame(self.master, padding=(8, 0, 8, 4))
        opts.pack(fill="x")
        ttk.Label(opts, text="Mindestgröße (KB):").pack(side="left")
        ttk.Spinbox(opts, from_=0, to=10_000_000, width=8, textvariable=self.min_size_kb).pack(side="left", padx=4)
        ttk.Checkbutton(opts, text="Versteckte Dateien einbeziehen", variable=self.include_hidden).pack(side="left", padx=12)
        ttk.Checkbutton(opts, text="In den Papierkorb verschieben statt endgültig löschen",
                        variable=self.use_trash).pack(side="left")

        tree_frame = ttk.Frame(self.master, padding=(8, 0))
        tree_frame.pack(fill="both", expand=True)
        columns = ("mark", "size", "modified", "path")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Gruppe")
        self.tree.heading("mark", text="Markiert")
        self.tree.heading("size", text="Größe")
        self.tree.heading("modified", text="Geändert")
        self.tree.heading("path", text="Pfad")
        self.tree.column("#0", width=140, stretch=False)
        self.tree.column("mark", width=70, anchor="center", stretch=False)
        self.tree.column("size", width=100, anchor="e", stretch=False)
        self.tree.column("modified", width=140, stretch=False)
        self.tree.column("path", width=500)
        self.tree.tag_configure("group", background="#e8e8e8", font=("TkDefaultFont", 10, "bold"))
        self.tree.tag_configure("checked", foreground="#b00020")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self.toggle_selected())
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Delete>", lambda e: self.delete_checked())

        actions = ttk.Frame(self.master, padding=8)
        actions.pack(fill="x")
        ttk.Button(actions, text="Alle außer erster markieren", command=self.mark_all_but_first).pack(side="left")
        ttk.Button(actions, text="Ältere markieren (neueste behalten)", command=self.mark_older).pack(side="left", padx=4)
        ttk.Button(actions, text="Markierung aufheben", command=self.unmark_all).pack(side="left")
        ttk.Button(actions, text="Ordner öffnen", command=self.open_folder).pack(side="left", padx=4)
        self.delete_button = ttk.Button(actions, text="Markierte löschen", command=self.delete_checked)
        self.delete_button.pack(side="right")

        bottom = ttk.Frame(self.master, padding=(8, 0, 8, 8))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=200)
        self.progress.pack(side="right")
        ttk.Label(bottom, textvariable=self.status, anchor="w").pack(side="left", fill="x", expand=True)

    def choose_directory(self):
        path = filedialog.askdirectory(initialdir=self.directory.get() or os.path.expanduser("~"),
                                       title="Verzeichnis auswählen")
        if path:
            self.directory.set(path)

    # ---------- Suche ----------
    def start_scan(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status.set("Abbruch angefordert …")
            return
        root = self.directory.get().strip()
        if not os.path.isdir(root):
            messagebox.showerror("Fehler", f"„{root}“ ist kein gültiges Verzeichnis.")
            return
        try:
            min_size = max(1, int(float(self.min_size_kb.get().replace(",", ".")) * 1024))
        except ValueError:
            messagebox.showerror("Fehler", "Ungültige Mindestgröße.")
            return

        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        self.item_info.clear()
        self.cancel_event = threading.Event()
        self.scan_button.configure(text="Abbrechen")
        self.progress.configure(value=0)
        self.status.set("Suche läuft …")
        include_hidden = self.include_hidden.get()

        def progress(msg, frac=None):
            self.queue.put(("progress", msg, frac))

        def run():
            start = time.monotonic()
            try:
                groups = find_duplicates(root, min_size, include_hidden, progress, self.cancel_event)
                self.queue.put(("done", groups, time.monotonic() - start))
            except Exception as exc:  # noqa: BLE001 – Fehler in der GUI anzeigen
                self.queue.put(("error", str(exc), None))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _poll_queue(self):
        try:
            while True:
                kind, a, b = self.queue.get_nowait()
                if kind == "progress":
                    self.status.set(a)
                    if b is not None:
                        self.progress.configure(value=b * 100)
                elif kind == "done":
                    self.scan_button.configure(text="Suchen")
                    self.progress.configure(value=100)
                    if self.cancel_event.is_set():
                        self.status.set("Suche abgebrochen.")
                    else:
                        self._show_results(a, b)
                elif kind == "error":
                    self.scan_button.configure(text="Suchen")
                    messagebox.showerror("Fehler", a)
        except queue.Empty:
            pass
        self.master.after(100, self._poll_queue)

    def _show_results(self, groups, elapsed):
        for i, paths in enumerate(groups, 1):
            size = os.path.getsize(paths[0]) if os.path.exists(paths[0]) else 0
            gid = self.tree.insert("", "end", text=f"Gruppe {i} ({len(paths)}×)",
                                   values=("", human_size(size), "", f"{len(paths)} identische Dateien"),
                                   open=True, tags=("group",))
            for path in paths:
                try:
                    mtime = time.strftime("%d.%m.%Y %H:%M", time.localtime(os.path.getmtime(path)))
                except OSError:
                    mtime = "?"
                iid = self.tree.insert(gid, "end", text="", values=(UNCHECKED, human_size(size), mtime, path))
                self.item_info[iid] = (path, size)
        self._update_status(elapsed=elapsed)

    def _update_status(self, elapsed=None):
        groups = self.tree.get_children()
        files = sum(len(self.tree.get_children(g)) for g in groups)
        wasted = sum(self.item_info[c][1] * (len(self.tree.get_children(g)) - 1)
                     for g in groups for c in self.tree.get_children(g)[:1])
        marked = sum(self.item_info[i][1] for i in self.checked)
        text = (f"{len(groups)} Gruppen, {files} Dateien, {human_size(wasted)} belegt durch Duplikate"
                f" | Markiert: {len(self.checked)} Dateien ({human_size(marked)})")
        if elapsed is not None:
            text += f" | Dauer: {elapsed:.1f} s"
        self.status.set(text)

    # ---------- Markieren ----------
    def _set_checked(self, iid, state):
        if iid not in self.item_info:
            return
        if state:
            self.checked.add(iid)
            self.tree.set(iid, "mark", CHECKED)
            self.tree.item(iid, tags=("checked",))
        else:
            self.checked.discard(iid)
            self.tree.set(iid, "mark", UNCHECKED)
            self.tree.item(iid, tags=())

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid in self.item_info:
            self._set_checked(iid, iid not in self.checked)
            self._update_status()
            return "break"

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid in self.item_info:
            self._open(self.item_info[iid][0])

    def toggle_selected(self):
        for iid in self.tree.selection():
            if iid in self.item_info:
                self._set_checked(iid, iid not in self.checked)
            else:  # Gruppenzeile: alle Kinder umschalten
                for child in self.tree.get_children(iid):
                    self._set_checked(child, child not in self.checked)
        self._update_status()

    def mark_all_but_first(self):
        for gid in self.tree.get_children():
            children = self.tree.get_children(gid)
            for n, iid in enumerate(children):
                self._set_checked(iid, n > 0)
        self._update_status()

    def mark_older(self):
        for gid in self.tree.get_children():
            children = list(self.tree.get_children(gid))

            def mtime(iid):
                try:
                    return os.path.getmtime(self.item_info[iid][0])
                except OSError:
                    return 0

            newest = max(children, key=mtime)
            for iid in children:
                self._set_checked(iid, iid != newest)
        self._update_status()

    def unmark_all(self):
        for iid in list(self.checked):
            self._set_checked(iid, False)
        self._update_status()

    # ---------- Öffnen ----------
    def _open(self, path):
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_folder(self):
        for iid in self.tree.selection():
            if iid in self.item_info:
                self._open(os.path.dirname(self.item_info[iid][0]))
                return
        messagebox.showinfo("Hinweis", "Bitte zuerst eine Datei in der Liste auswählen.")

    # ---------- Löschen ----------
    def delete_checked(self):
        if not self.checked:
            messagebox.showinfo("Hinweis", "Keine Dateien markiert.")
            return

        full_groups = [gid for gid in self.tree.get_children()
                       if all(c in self.checked for c in self.tree.get_children(gid))]
        if full_groups:
            names = ", ".join(self.tree.item(g, "text") for g in full_groups[:5])
            if not messagebox.askyesno(
                    "Warnung",
                    f"In {len(full_groups)} Gruppe(n) sind ALLE Kopien markiert ({names}…).\n"
                    "Damit würde die Datei komplett verloren gehen.\n\nTrotzdem fortfahren?",
                    icon="warning"):
                return

        trash = self.use_trash.get()
        total = sum(self.item_info[i][1] for i in self.checked)
        action = "in den Papierkorb verschieben" if trash else "ENDGÜLTIG löschen"
        if not messagebox.askyesno("Löschen bestätigen",
                                   f"{len(self.checked)} Datei(en) ({human_size(total)}) {action}?"):
            return

        errors = []
        deleted = 0
        for iid in list(self.checked):
            path = self.item_info[iid][0]
            try:
                if trash:
                    move_to_trash(path)
                else:
                    os.remove(path)
            except (OSError, subprocess.CalledProcessError) as exc:
                errors.append(f"{path}: {exc}")
                continue
            deleted += 1
            parent = self.tree.parent(iid)
            self.checked.discard(iid)
            del self.item_info[iid]
            self.tree.delete(iid)
            remaining = self.tree.get_children(parent)
            if len(remaining) < 2:
                for child in remaining:
                    self.checked.discard(child)
                    self.item_info.pop(child, None)
                self.tree.delete(parent)
            else:
                text = self.tree.item(parent, "text").rsplit("(", 1)[0]
                self.tree.item(parent, text=f"{text}({len(remaining)}×)")

        self._update_status()
        if errors:
            messagebox.showerror("Fehler beim Löschen",
                                 f"{deleted} gelöscht, {len(errors)} Fehler:\n\n" + "\n".join(errors[:10]))
        else:
            messagebox.showinfo("Fertig", f"{deleted} Datei(en) gelöscht ({human_size(total)}).")


def main():
    root = tk.Tk()
    app = DuplicateFinderApp(root)
    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        app.directory.set(os.path.abspath(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
