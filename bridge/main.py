"""
Avigilon Unity Chrome Plugin Bridge — main entry point.

Starts the localhost HTTP server and system tray icon.
The server proxies requests from the Chrome extension to the
Avigilon server, handling SSL bypass and XML parsing.

Usage:
  python main.py                 # Launch with tray icon + settings window
  python main.py --background    # Launch headless (auto-start mode)
"""

import argparse
import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from src.constants import BRIDGE_PORT, VERSION, CONFIG_DIR, ensure_config_dir
from src.config import load_config, save_config
from src.server import run_server
from src.tray import TrayIcon
from src.autostart import enable_autostart, disable_autostart, is_autostart_enabled

# Logging
ensure_config_dir()
LOG_FILE = os.path.join(CONFIG_DIR, "bridge.log")


class TkLogHandler(logging.Handler):
    """Logging handler that appends to a Tk Text widget."""

    def __init__(self, text_widget: tk.Text):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        try:
            self.text_widget.after(0, self._append, msg)
        except Exception:
            pass

    def _append(self, msg):
        self.text_widget.configure(state=tk.NORMAL)
        self.text_widget.insert(tk.END, msg + '\n')
        self.text_widget.see(tk.END)
        # Keep max 500 lines
        line_count = int(self.text_widget.index('end-1c').split('.')[0])
        if line_count > 500:
            self.text_widget.delete('1.0', f'{line_count - 500}.0')
        self.text_widget.configure(state=tk.DISABLED)


log_format = '%(asctime)s [%(name)s] %(levelname)s: %(message)s'

logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ],
)
logger = logging.getLogger(__name__)


class SettingsWindow:
    """Tk settings window for configuring the bridge.

    Only Avigilon credentials are configured here — AccessGrid credentials
    are configured in the Chrome extension popup (the extension calls
    the AG API directly, the bridge never talks to AccessGrid).
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Avigilon Bridge v{VERSION}")
        self.root.geometry("560x660")
        self.root.resizable(True, True)

        config = load_config()

        # Scrollable main frame
        canvas = tk.Canvas(root)
        scrollbar = ttk.Scrollbar(root, orient=tk.VERTICAL, command=canvas.yview)
        main_frame = ttk.Frame(canvas, padding=20)

        main_frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=main_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- Avigilon section ---
        ttk.Label(main_frame, text="Avigilon Unity", font=('', 13, 'bold')).pack(anchor=tk.W)
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(2, 10))

        avigilon = config.get('avigilon', {})

        ttk.Label(main_frame, text="Host / IP:").pack(anchor=tk.W)
        self.avigilon_host = ttk.Entry(main_frame, width=50)
        self.avigilon_host.insert(0, avigilon.get('host', ''))
        self.avigilon_host.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(main_frame, text="Username:").pack(anchor=tk.W)
        self.avigilon_user = ttk.Entry(main_frame, width=50)
        self.avigilon_user.insert(0, avigilon.get('username', ''))
        self.avigilon_user.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(main_frame, text="Password:").pack(anchor=tk.W)
        self.avigilon_pass = ttk.Entry(main_frame, width=50, show='*')
        self.avigilon_pass.insert(0, avigilon.get('password', ''))
        self.avigilon_pass.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(main_frame, text="Test Avigilon Connection", command=self._test_avigilon).pack(anchor=tk.W, pady=(0, 16))

        # --- Info ---
        ttk.Label(
            main_frame,
            text="AccessGrid credentials are configured in the Chrome extension popup.",
            foreground='#6b7280', font=('', 11),
        ).pack(anchor=tk.W, pady=(0, 12))

        # --- Auto-start ---
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        ttk.Checkbutton(
            main_frame, text="Start bridge on login",
            variable=self.autostart_var,
        ).pack(anchor=tk.W, pady=(0, 12))

        # --- Status ---
        self.status_label = ttk.Label(
            main_frame,
            text=f"Bridge running on localhost:{BRIDGE_PORT}",
            foreground='green',
        )
        self.status_label.pack(anchor=tk.W, pady=(0, 8))

        # --- Buttons ---
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 16))

        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_frame, text="Cancel", command=self._hide).pack(side=tk.RIGHT)

        # --- Log viewer ---
        ttk.Label(main_frame, text="Log", font=('', 13, 'bold')).pack(anchor=tk.W)
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(2, 8))

        log_frame = ttk.Frame(main_frame)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_frame, height=12, wrap=tk.NONE,
            font=('Menlo', 10) if sys.platform == 'darwin' else ('Consolas', 9),
            bg='#1e1e1e', fg='#d4d4d4', insertbackground='#d4d4d4',
            state=tk.DISABLED, relief=tk.FLAT, padx=8, pady=8,
        )
        log_scroll_y = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll_x = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=log_scroll_y.set, xscrollcommand=log_scroll_x.set)

        self.log_text.grid(row=0, column=0, sticky='nsew')
        log_scroll_y.grid(row=0, column=1, sticky='ns')
        log_scroll_x.grid(row=1, column=0, sticky='ew')
        log_frame.grid_rowconfigure(0, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        ttk.Button(main_frame, text="Copy Log", command=self._copy_log).pack(anchor=tk.W, pady=(8, 0))

        # Attach log handler
        self._log_handler = TkLogHandler(self.log_text)
        self._log_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(self._log_handler)

    def _save(self):
        config = load_config()
        config['avigilon'] = {
            'host': self.avigilon_host.get().strip(),
            'username': self.avigilon_user.get().strip(),
            'password': self.avigilon_pass.get(),
        }
        save_config(config)

        if self.autostart_var.get():
            enable_autostart()
        else:
            disable_autostart()

        # Reset the server's cached client
        from src.server import _reset_client
        _reset_client()

        messagebox.showinfo("Saved", "Configuration saved successfully.")

    def _test_avigilon(self):
        from src.avigilon_client import AvigilonClient
        host = self.avigilon_host.get().strip()
        user = self.avigilon_user.get().strip()
        pwd = self.avigilon_pass.get()
        if not host or not user or not pwd:
            messagebox.showwarning("Missing", "Fill in host, username, and password first.")
            return
        try:
            client = AvigilonClient(host, user, pwd)
            ok = client.test_connection()
            if ok:
                messagebox.showinfo("Success", "Connected to Avigilon successfully!")
            else:
                messagebox.showerror("Failed", "Could not connect. Check credentials and host.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _copy_log(self):
        self.log_text.configure(state=tk.NORMAL)
        content = self.log_text.get('1.0', tk.END).strip()
        self.log_text.configure(state=tk.DISABLED)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("Copied", "Log copied to clipboard.")

    def _hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()


def main():
    parser = argparse.ArgumentParser(description="Avigilon Bridge")
    parser.add_argument('--background', action='store_true', help="Run headless (no settings window)")
    parser.add_argument('--port', type=int, default=BRIDGE_PORT, help="HTTP server port")
    args = parser.parse_args()

    logger.info(f"Avigilon Bridge v{VERSION} starting on port {args.port}")

    # Start HTTP server in background thread
    server_thread = threading.Thread(
        target=run_server,
        kwargs={'port': args.port},
        daemon=True,
        name="BridgeHTTPServer",
    )
    server_thread.start()
    logger.info(f"HTTP server started on localhost:{args.port}")

    if args.background:
        # Headless mode — just tray icon, no window
        tray = TrayIcon(on_quit=lambda: sys.exit(0))
        tray.start()
        # Keep main thread alive
        server_thread.join()
    else:
        # GUI mode — show settings window
        root = tk.Tk()
        settings = SettingsWindow(root)

        tray = TrayIcon(
            on_settings=lambda: root.after(0, settings.show),
            on_quit=lambda: root.after(0, root.destroy),
        )
        tray.start()

        # Allow closing window to minimize to tray
        root.protocol('WM_DELETE_WINDOW', lambda: root.withdraw())
        root.mainloop()


if __name__ == '__main__':
    main()
