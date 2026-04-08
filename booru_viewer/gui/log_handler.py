"""Qt-aware logging handler that emits log lines to a QTextEdit."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QTextEdit


class LogHandler(logging.Handler, QObject):
    """Logging handler that emits to a QTextEdit."""

    log_signal = Signal(str)

    def __init__(self, widget: QTextEdit) -> None:
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self._widget = widget
        self.log_signal.connect(self._append)
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self.log_signal.emit(msg)

    def _append(self, msg: str) -> None:
        self._widget.append(msg)
        sb = self._widget.verticalScrollBar()
        sb.setValue(sb.maximum())
