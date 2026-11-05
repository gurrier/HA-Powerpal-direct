# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Constants for the Powerpal Direct integration."""

from __future__ import annotations

DOMAIN = "powerpal_direct"
MANUFACTURER = "Powerpal"

CONF_PAIRING_CODE = "pairing_code"
CONF_PULSES_PER_KWH = "pulses_per_kwh"
CONF_NOTIFICATION_INTERVAL = "notification_interval"
CONF_POLL_INTERVAL = "poll_interval"
CONF_COST_PER_KWH = "cost_per_kwh"

DEFAULT_PULSES_PER_KWH = 1000.0
DEFAULT_NOTIFICATION_INTERVAL = 1
# Minutes between catch-up connections. 0 stays connected, like the ESP32 gateway did.
DEFAULT_POLL_INTERVAL = 0
DEFAULT_COST_PER_KWH = 0.0
POLL_INTERVAL_MAX = 24 * 60

STORAGE_VERSION = 1
