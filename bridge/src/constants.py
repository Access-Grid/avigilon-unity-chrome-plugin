"""
Centralized constants for Avigilon Unity Chrome Plugin Bridge.
"""

import os
import sys

VERSION = "1.0.0"
APP_NAME = "AvigilonBridge"
BRIDGE_PORT = 19780

# Config directory - platform-aware
if sys.platform == 'win32':
    CONFIG_DIR = os.path.join(
        os.path.expanduser("~"), "AppData", "Local", APP_NAME
    )
elif sys.platform == 'darwin':
    CONFIG_DIR = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", APP_NAME
    )
else:
    CONFIG_DIR = os.path.join(
        os.path.expanduser("~"), ".config", "avigilon-bridge"
    )


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Avigilon identity status codes
AVIGILON_IDENTITY_STATUS_ACTIVE = "1"
AVIGILON_IDENTITY_STATUS_INACTIVE = "2"

# Avigilon token status codes
AVIGILON_TOKEN_STATUS_ACTIVE = "1"
AVIGILON_TOKEN_STATUS_INACTIVE = "2"
AVIGILON_TOKEN_STATUS_NOT_YET_ACTIVE = "3"
AVIGILON_TOKEN_STATUS_EXPIRED = "4"

# Avigilon token type
AVIGILON_TOKEN_TYPE_STANDARD = "0"

# Status mappings
AVIGILON_TO_AG_STATUS = {
    "1": "active",
    "2": "suspended",
    "3": "suspended",
    "4": "suspended",
}

AG_TO_AVIGILON_STATUS = {
    "active": "1",
    "suspended": "2",
    "created": "1",
}

# HTTP settings
HTTP_TIMEOUT = 30
HTTP_USER_AGENT = "AvigilonBridge/1.0"
