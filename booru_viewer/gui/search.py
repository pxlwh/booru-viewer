"""Search bar with tag autocomplete, history, and saved searches."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QStringListModel
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QCompleter,
    QMenu,
    QInputDialog,
)

from ..core.db import Database


class SearchBar(QWidget):
    """Tag search bar with autocomplete, history dropdown, and saved searches."""

    search_requested = Signal(str)
    autocomplete_requested = Signal(str)

    def __init__(self, db: Database | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # History button
        self._history_btn = QPushButton("v")
        self._history_btn.setFixedWidth(30)
        self._history_btn.setToolTip("Search history & saved searches")
        self._history_btn.clicked.connect(self._show_history_menu)
        layout.addWidget(self._history_btn)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Search tags... (supports -negatives)")
        self._input.returnPressed.connect(self._do_search)
        layout.addWidget(self._input, stretch=1)

        # Save search button
        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedWidth(50)
        self._save_btn.setToolTip("Save current search")
        self._save_btn.clicked.connect(self._save_current_search)
        layout.addWidget(self._save_btn)

        self._btn = QPushButton("Search")
        self._btn.clicked.connect(self._do_search)
        layout.addWidget(self._btn)

        # Autocomplete
        self._completer_model = QStringListModel()
        self._completer = QCompleter(self._completer_model)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._input.setCompleter(self._completer)

        # Debounce
        self._ac_timer = QTimer()
        self._ac_timer.setSingleShot(True)
        self._ac_timer.setInterval(300)
        self._ac_timer.timeout.connect(self._request_autocomplete)
        self._input.textChanged.connect(self._on_text_changed)

    def _on_text_changed(self, text: str) -> None:
        self._ac_timer.start()

    def _request_autocomplete(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        last_tag = text.split()[-1] if text.split() else ""
        query = last_tag.lstrip("-")
        if len(query) >= 2:
            self.autocomplete_requested.emit(query)

    def set_suggestions(self, suggestions: list[str]) -> None:
        self._completer_model.setStringList(suggestions)

    def _do_search(self) -> None:
        query = self._input.text().strip()
        if self._db and query:
            self._db.add_search_history(query)
        self.search_requested.emit(query)

    def _show_history_menu(self) -> None:
        if not self._db:
            return

        menu = QMenu(self)

        # Saved searches
        saved = self._db.get_saved_searches()
        if saved:
            saved_header = menu.addAction("-- Saved Searches --")
            saved_header.setEnabled(False)
            saved_actions = {}
            for sid, name, query in saved:
                a = menu.addAction(f"  {name}  ({query})")
                saved_actions[id(a)] = (sid, query)
            menu.addSeparator()

        # History
        history = self._db.get_search_history()
        if history:
            hist_header = menu.addAction("-- Recent --")
            hist_header.setEnabled(False)
            hist_actions = {}
            for query in history:
                a = menu.addAction(f"  {query}")
                hist_actions[id(a)] = query
            menu.addSeparator()
            clear_action = menu.addAction("Clear History")
        else:
            hist_actions = {}
            clear_action = None

        if not saved and not history:
            empty = menu.addAction("No history yet")
            empty.setEnabled(False)

        action = menu.exec(self._history_btn.mapToGlobal(self._history_btn.rect().bottomLeft()))
        if not action:
            return

        if clear_action and action == clear_action:
            self._db.clear_search_history()
        elif id(action) in hist_actions:
            self._input.setText(hist_actions[id(action)])
            self._do_search()
        elif saved and id(action) in saved_actions:
            _, query = saved_actions[id(action)]
            self._input.setText(query)
            self._do_search()

    def _save_current_search(self) -> None:
        if not self._db:
            return
        query = self._input.text().strip()
        if not query:
            return
        name, ok = QInputDialog.getText(self, "Save Search", "Name:", text=query)
        if ok and name.strip():
            self._db.add_saved_search(name.strip(), query)

    def text(self) -> str:
        return self._input.text().strip()

    def set_text(self, text: str) -> None:
        self._input.setText(text)

    def focus(self) -> None:
        self._input.setFocus()
