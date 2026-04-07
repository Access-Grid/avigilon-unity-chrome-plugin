"""
System tray icon for the Avigilon Bridge.

Provides a minimal tray presence so the bridge runs as a background app
with right-click options to open settings or quit.
"""

import logging
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    logger.warning("pystray/Pillow not available — tray icon disabled")


def _create_icon_image(size=64):
    """Generate a simple green circle icon."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=(34, 197, 94, 255),
        outline=(22, 163, 74, 255),
        width=2,
    )
    return img


class TrayIcon:
    """System tray icon wrapper."""

    def __init__(
        self,
        on_settings: Optional[Callable] = None,
        on_quit: Optional[Callable] = None,
    ):
        self.on_settings = on_settings
        self.on_quit = on_quit
        self._icon = None

    def start(self):
        if not HAS_TRAY:
            logger.info("Tray icon not available — running headless")
            return

        menu = pystray.Menu(
            pystray.MenuItem('Avigilon Bridge', None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Settings...', self._handle_settings),
            pystray.MenuItem('Quit', self._handle_quit),
        )

        self._icon = pystray.Icon(
            'avigilon-bridge',
            icon=_create_icon_image(),
            title='Avigilon Bridge - Running',
            menu=menu,
        )

        thread = threading.Thread(target=self._icon.run, daemon=True)
        thread.start()
        logger.info("Tray icon started")

    def stop(self):
        if self._icon:
            self._icon.stop()

    def _handle_settings(self, icon, item):
        if self.on_settings:
            self.on_settings()

    def _handle_quit(self, icon, item):
        if self.on_quit:
            self.on_quit()
        icon.stop()
        sys.exit(0)
