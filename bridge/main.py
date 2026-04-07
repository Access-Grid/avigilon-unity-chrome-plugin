"""
Avigilon Unity Chrome Plugin Bridge — main entry point.

Starts the localhost HTTP server and system tray icon.
The server proxies requests from the Chrome extension to the
Plasec/Avigilon server, handling SSL bypass and XML parsing.

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ],
)
logger = logging.getLogger(__name__)


class SettingsWindow:
    """Tk settings window for configuring the bridge."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Avigilon Bridge v{VERSION}")
        self.root.geometry("500x620")
        self.root.resizable(False, False)

        config = load_config()

        main_frame = ttk.Frame(root, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Plasec section ---
        ttk.Label(main_frame, text="Plasec / Avigilon Unity", font=('', 13, 'bold')).pack(anchor=tk.W)
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(2, 10))

        plasec = config.get('plasec', {})

        ttk.Label(main_frame, text="Host / IP:").pack(anchor=tk.W)
        self.plasec_host = ttk.Entry(main_frame, width=50)
        self.plasec_host.insert(0, plasec.get('host', ''))
        self.plasec_host.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(main_frame, text="Username:").pack(anchor=tk.W)
        self.plasec_user = ttk.Entry(main_frame, width=50)
        self.plasec_user.insert(0, plasec.get('username', ''))
        self.plasec_user.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(main_frame, text="Password:").pack(anchor=tk.W)
        self.plasec_pass = ttk.Entry(main_frame, width=50, show='*')
        self.plasec_pass.insert(0, plasec.get('password', ''))
        self.plasec_pass.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(main_frame, text="Test Plasec Connection", command=self._test_plasec).pack(anchor=tk.W, pady=(0, 16))

        # --- AccessGrid section ---
        ttk.Label(main_frame, text="AccessGrid", font=('', 13, 'bold')).pack(anchor=tk.W)
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(2, 10))

        ag = config.get('accessgrid', {})

        ttk.Label(main_frame, text="Account ID:").pack(anchor=tk.W)
        self.ag_account = ttk.Entry(main_frame, width=50)
        self.ag_account.insert(0, ag.get('account_id', ''))
        self.ag_account.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(main_frame, text="API Secret:").pack(anchor=tk.W)
        self.ag_secret = ttk.Entry(main_frame, width=50, show='*')
        self.ag_secret.insert(0, ag.get('api_secret', ''))
        self.ag_secret.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(main_frame, text="Template ID:").pack(anchor=tk.W)
        self.ag_template = ttk.Entry(main_frame, width=50)
        self.ag_template.insert(0, ag.get('template_id', ''))
        self.ag_template.pack(fill=tk.X, pady=(0, 16))

        # --- Auto-start ---
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        ttk.Checkbutton(
            main_frame, text="Start bridge on login",
            variable=self.autostart_var,
        ).pack(anchor=tk.W, pady=(0, 16))

        # --- Status ---
        self.status_label = ttk.Label(
            main_frame,
            text=f"Bridge running on localhost:{BRIDGE_PORT}",
            foreground='green',
        )
        self.status_label.pack(anchor=tk.W, pady=(0, 8))

        # --- Buttons ---
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_frame, text="Cancel", command=self._hide).pack(side=tk.RIGHT)

    def _save(self):
        config = {
            'plasec': {
                'host': self.plasec_host.get().strip(),
                'username': self.plasec_user.get().strip(),
                'password': self.plasec_pass.get(),
            },
            'accessgrid': {
                'account_id': self.ag_account.get().strip(),
                'api_secret': self.ag_secret.get(),
                'template_id': self.ag_template.get().strip(),
            },
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

    def _test_plasec(self):
        from src.plasec_client import PlaSecClient
        host = self.plasec_host.get().strip()
        user = self.plasec_user.get().strip()
        pwd = self.plasec_pass.get()
        if not host or not user or not pwd:
            messagebox.showwarning("Missing", "Fill in host, username, and password first.")
            return
        try:
            client = PlaSecClient(host, user, pwd)
            ok = client.test_connection()
            if ok:
                messagebox.showinfo("Success", "Connected to Plasec successfully!")
            else:
                messagebox.showerror("Failed", "Could not connect. Check credentials and host.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

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
