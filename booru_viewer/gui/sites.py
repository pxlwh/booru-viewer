"""Site manager dialog."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QMessageBox,
    QWidget,
)

from ..core.db import Database, Site
from ..core.api.detect import detect_site_type
from ..core.concurrency import run_on_app_loop


class SiteDialog(QDialog):
    """Dialog to add or edit a booru site."""

    # Internal signals used to marshal worker results back to the GUI thread.
    # Connected with QueuedConnection so emit() from the asyncio loop thread
    # is always delivered on the Qt main thread.
    _detect_done_sig = Signal(object, object)  # (result_or_None, error_or_None)
    _test_done_sig = Signal(bool, str)

    def __init__(self, parent: QWidget | None = None, site: Site | None = None) -> None:
        super().__init__(parent)
        self._editing = site is not None
        self.setWindowTitle("Edit Site" if self._editing else "Add Site")
        self.setMinimumWidth(400)
        # Set when the dialog is closed/destroyed so in-flight worker
        # callbacks can short-circuit instead of poking a dead QObject.
        self._closed = False
        # Tracked so we can cancel pending coroutines on close.
        self._inflight = []  # list[concurrent.futures.Future]
        self._detect_done_sig.connect(
            self._detect_finished, Qt.ConnectionType.QueuedConnection
        )
        self._test_done_sig.connect(
            self._test_finished, Qt.ConnectionType.QueuedConnection
        )

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("e.g. Danbooru")
        form.addRow("Name:", self._name_input)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("e.g. https://gelbooru.com or paste a full post URL")
        self._url_input.textChanged.connect(self._try_parse_url)
        form.addRow("URL:", self._url_input)

        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("(optional — or paste full &api_key=...&user_id=... string)")
        self._key_input.textChanged.connect(self._try_parse_credentials)
        form.addRow("API Key:", self._key_input)

        self._user_input = QLineEdit()
        self._user_input.setPlaceholderText("(optional)")
        form.addRow("API User:", self._user_input)

        layout.addLayout(form)

        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        btns = QHBoxLayout()
        self._detect_btn = QPushButton("Auto-Detect")
        self._detect_btn.clicked.connect(self._on_detect)
        btns.addWidget(self._detect_btn)

        self._test_btn = QPushButton("Test")
        self._test_btn.clicked.connect(self._on_test)
        btns.addWidget(self._test_btn)

        btns.addStretch()

        save_btn = QPushButton("Save" if self._editing else "Add")
        save_btn.clicked.connect(self.accept)
        btns.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)

        layout.addLayout(btns)

        self._detected_type: str | None = None

        # Populate fields if editing
        if site:
            self._name_input.setText(site.name)
            self._url_input.setText(site.url)
            self._key_input.setText(site.api_key or "")
            self._user_input.setText(site.api_user or "")
            self._detected_type = site.api_type
            self._status_label.setText(f"Type: {site.api_type}")

    def _on_detect(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            self._status_label.setText("Enter a URL first.")
            return
        self._status_label.setText("Detecting...")
        self._detect_btn.setEnabled(False)

        api_key = self._key_input.text().strip() or None
        api_user = self._user_input.text().strip() or None

        async def _do_detect():
            try:
                result = await detect_site_type(url, api_key=api_key, api_user=api_user)
                if not self._closed:
                    self._detect_done_sig.emit(result, None)
            except Exception as e:
                if not self._closed:
                    self._detect_done_sig.emit(None, e)

        fut = run_on_app_loop(_do_detect())
        self._inflight.append(fut)
        fut.add_done_callback(lambda f: self._inflight.remove(f) if f in self._inflight else None)

    def _detect_finished(self, result, error) -> None:
        if self._closed:
            return
        self._detect_btn.setEnabled(True)
        if error:
            self._status_label.setText(f"Error: {error}")
        elif result:
            self._detected_type = result
            self._status_label.setText(f"Detected: {result}")
        else:
            self._status_label.setText("Could not detect API type.")

    def _on_test(self) -> None:
        url = self._url_input.text().strip()
        api_type = self._detected_type or "danbooru"
        api_key = self._key_input.text().strip() or None
        api_user = self._user_input.text().strip() or None
        if not url:
            self._status_label.setText("Enter a URL first.")
            return
        self._status_label.setText("Testing connection...")
        self._test_btn.setEnabled(False)

        from ..core.api.detect import client_for_type

        async def _do_test():
            try:
                client = client_for_type(api_type, url, api_key=api_key, api_user=api_user)
                ok, detail = await client.test_connection()
                if not self._closed:
                    self._test_done_sig.emit(ok, detail)
            except Exception as e:
                if not self._closed:
                    self._test_done_sig.emit(False, str(e))

        fut = run_on_app_loop(_do_test())
        self._inflight.append(fut)
        fut.add_done_callback(lambda f: self._inflight.remove(f) if f in self._inflight else None)

    def _test_finished(self, ok: bool, detail: str) -> None:
        if self._closed:
            return
        self._test_btn.setEnabled(True)
        if ok:
            self._status_label.setText(f"Connected! {detail}")
        else:
            self._status_label.setText(f"Failed: {detail}")

    def closeEvent(self, event) -> None:
        # Mark closed first so in-flight callbacks short-circuit, then
        # cancel anything still pending so we don't tie up the loop.
        self._closed = True
        for fut in list(self._inflight):
            try:
                fut.cancel()
            except Exception:
                pass
        super().closeEvent(event)

    def _try_parse_url(self, text: str) -> None:
        """Strip query params from pasted URLs like https://gelbooru.com/index.php?page=post&s=list&tags=all."""
        from urllib.parse import urlparse
        text = text.strip()
        if "?" not in text:
            return
        try:
            parsed = urlparse(text)
            base = f"{parsed.scheme}://{parsed.netloc}"
            if not parsed.scheme or not parsed.netloc:
                return
            self._url_input.blockSignals(True)
            self._url_input.setText(base)
            self._url_input.blockSignals(False)
            self._status_label.setText(f"Extracted base URL: {base}")
        except Exception:
            pass

    def _try_parse_credentials(self, text: str) -> None:
        """Auto-parse combined credential strings like &api_key=XXX&user_id=123."""
        import re
        # Match user_id regardless of api_key being present
        user_match = re.search(r'user_id=([^&\s]+)', text)
        key_match = re.search(r'api_key=([^&\s]+)', text)
        if user_match:
            self._user_input.setText(user_match.group(1))
            if key_match:
                self._key_input.blockSignals(True)
                self._key_input.setText(key_match.group(1))
                self._key_input.blockSignals(False)
                self._status_label.setText("Parsed api_key and user_id")
            else:
                # Clear the pasted junk, user needs to enter key separately
                self._key_input.blockSignals(True)
                self._key_input.clear()
                self._key_input.blockSignals(False)
                self._status_label.setText("Parsed user_id={}. Paste your API key above.".format(user_match.group(1)))

    @property
    def site_data(self) -> dict:
        return {
            "name": self._name_input.text().strip(),
            "url": self._url_input.text().strip(),
            "api_type": self._detected_type or "danbooru",
            "api_key": self._key_input.text().strip() or None,
            "api_user": self._user_input.text().strip() or None,
        }


class SiteManagerDialog(QDialog):
    """Dialog to manage booru sites."""

    sites_changed = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Manage Sites")
        self.setMinimumSize(500, 350)

        layout = QVBoxLayout(self)

        self._list = QListWidget()
        layout.addWidget(self._list)

        btns = QHBoxLayout()
        add_btn = QPushButton("Add Site")
        add_btn.clicked.connect(self._on_add)
        btns.addWidget(add_btn)

        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._on_edit)
        btns.addWidget(edit_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove)
        btns.addWidget(remove_btn)

        btns.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)

        layout.addLayout(btns)
        self._list.itemDoubleClicked.connect(lambda _: self._on_edit())
        self._refresh_list()

    def _refresh_list(self) -> None:
        self._list.clear()
        for site in self._db.get_sites(enabled_only=False):
            item = QListWidgetItem(f"{site.name}  [{site.api_type}]  {site.url}")
            item.setData(Qt.ItemDataRole.UserRole, site.id)
            self._list.addItem(item)

    def _on_add(self) -> None:
        dlg = SiteDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.site_data
            if not data["name"] or not data["url"]:
                QMessageBox.warning(self, "Error", "Name and URL are required.")
                return
            try:
                self._db.add_site(**data)
                self._refresh_list()
                self.sites_changed.emit()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _on_edit(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        site_id = item.data(Qt.ItemDataRole.UserRole)
        sites = self._db.get_sites(enabled_only=False)
        site = next((s for s in sites if s.id == site_id), None)
        if not site:
            return
        dlg = SiteDialog(self, site=site)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.site_data
            if not data["name"] or not data["url"]:
                QMessageBox.warning(self, "Error", "Name and URL are required.")
                return
            try:
                self._db.update_site(site_id, **data)
                self._refresh_list()
                self.sites_changed.emit()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _on_remove(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        site_id = item.data(Qt.ItemDataRole.UserRole)
        reply = QMessageBox.question(
            self, "Confirm", "Remove this site and all its bookmarks?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._db.delete_site(site_id)
            self._refresh_list()
            self.sites_changed.emit()
