"""
Platform-specific auto-start registration.

Registers the bridge binary to start automatically on login:
  - macOS:   ~/Library/LaunchAgents plist
  - Windows: Registry Run key
  - Linux:   XDG autostart .desktop file
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)


def get_executable_path() -> str:
    """Get the path to the current executable (works with PyInstaller)."""
    if getattr(sys, 'frozen', False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def enable_autostart():
    """Register the bridge to start on login."""
    exe = get_executable_path()
    if sys.platform == 'darwin':
        _enable_macos(exe)
    elif sys.platform == 'win32':
        _enable_windows(exe)
    else:
        _enable_linux(exe)


def disable_autostart():
    """Remove the auto-start registration."""
    if sys.platform == 'darwin':
        _disable_macos()
    elif sys.platform == 'win32':
        _disable_windows()
    else:
        _disable_linux()


def is_autostart_enabled() -> bool:
    if sys.platform == 'darwin':
        return os.path.exists(_macos_plist_path())
    elif sys.platform == 'win32':
        return _windows_key_exists()
    else:
        return os.path.exists(_linux_desktop_path())


# ------------------------------------------------------------------
# macOS
# ------------------------------------------------------------------

_PLIST_LABEL = 'com.accessgrid.avigilon-bridge'


def _macos_plist_path() -> str:
    return os.path.expanduser(f'~/Library/LaunchAgents/{_PLIST_LABEL}.plist')


def _enable_macos(exe: str):
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
        <string>--background</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""
    path = _macos_plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(plist)
    logger.info(f"macOS LaunchAgent written: {path}")


def _disable_macos():
    path = _macos_plist_path()
    if os.path.exists(path):
        os.remove(path)
        logger.info(f"macOS LaunchAgent removed: {path}")


# ------------------------------------------------------------------
# Windows
# ------------------------------------------------------------------

_WIN_KEY_NAME = 'AvigilonBridge'


def _enable_windows(exe: str):
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run',
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, _WIN_KEY_NAME, 0, winreg.REG_SZ, f'"{exe}" --background')
        winreg.CloseKey(key)
        logger.info("Windows Run key set")
    except Exception as e:
        logger.error(f"Failed to set Windows Run key: {e}")


def _disable_windows():
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run',
            0, winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, _WIN_KEY_NAME)
        winreg.CloseKey(key)
        logger.info("Windows Run key removed")
    except Exception as e:
        logger.error(f"Failed to remove Windows Run key: {e}")


def _windows_key_exists() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run',
            0, winreg.KEY_READ,
        )
        winreg.QueryValueEx(key, _WIN_KEY_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------
# Linux
# ------------------------------------------------------------------

def _linux_desktop_path() -> str:
    return os.path.expanduser('~/.config/autostart/avigilon-bridge.desktop')


def _enable_linux(exe: str):
    desktop = f"""[Desktop Entry]
Type=Application
Name=Avigilon Bridge
Exec={exe} --background
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
"""
    path = _linux_desktop_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(desktop)
    logger.info(f"Linux autostart written: {path}")


def _disable_linux():
    path = _linux_desktop_path()
    if os.path.exists(path):
        os.remove(path)
        logger.info(f"Linux autostart removed: {path}")
