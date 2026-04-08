"""GUI entry point."""

import os
import sys


def main() -> None:
    # Windows: set App User Model ID so taskbar pinning works
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                u"pax.booru-viewer.gui.1"
            )
        except Exception:
            pass

    # Apply file dialog platform setting before Qt initializes
    if sys.platform != "win32":
        try:
            from booru_viewer.core.db import Database
            db = Database()
            platform = db.get_setting("file_dialog_platform")
            db.close()
            if platform == "gtk":
                # Use xdg-desktop-portal which routes to GTK portal (Thunar)
                os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")
        except Exception as e:
            # Surface DB-init failures to stderr — silently swallowing meant
            # users debugging "why is my file picker the wrong one" had no
            # signal at all when the DB was missing or corrupt.
            print(
                f"booru-viewer: file_dialog_platform DB probe failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    from booru_viewer.gui.app_runtime import run
    run()


if __name__ == "__main__":
    main()
