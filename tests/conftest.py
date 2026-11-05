# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Fixtures for Powerpal Direct tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/ in every test."""
