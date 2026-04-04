"""Native file dialog wrappers. Uses zenity on Linux when GTK dialogs are preferred."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QWidget

from ..core.config import IS_WINDOWS


def _use_gtk() -> bool:
    if IS_WINDOWS:
        return False
    try:
        from ..core.db import Database
        db = Database()
        val = db.get_setting("file_dialog_platform")
        db.close()
        return val == "gtk"
    except Exception:
        return False


def save_file(
    parent: QWidget | None,
    title: str,
    default_name: str,
    filter_str: str,
) -> str | None:
    """Show a save file dialog. Returns path or None."""
    if _use_gtk():
        try:
            result = subprocess.run(
                [
                    "zenity", "--file-selection", "--save",
                    "--title", title,
                    "--filename", default_name,
                    "--confirm-overwrite",
                ],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except FileNotFoundError:
            pass  # zenity not installed, fall through to Qt

    path, _ = QFileDialog.getSaveFileName(parent, title, default_name, filter_str)
    return path or None


def open_file(
    parent: QWidget | None,
    title: str,
    filter_str: str,
) -> str | None:
    """Show an open file dialog. Returns path or None."""
    if _use_gtk():
        try:
            result = subprocess.run(
                [
                    "zenity", "--file-selection",
                    "--title", title,
                ],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except FileNotFoundError:
            pass

    path, _ = QFileDialog.getOpenFileName(parent, title, "", filter_str)
    return path or None


def select_directory(
    parent: QWidget | None,
    title: str,
) -> str | None:
    """Show a directory picker. Returns path or None."""
    if _use_gtk():
        try:
            result = subprocess.run(
                [
                    "zenity", "--file-selection", "--directory",
                    "--title", title,
                ],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except FileNotFoundError:
            pass

    path = QFileDialog.getExistingDirectory(parent, title)
    return path or None
