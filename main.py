"""Entry point for AutoDuck."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from autoduck.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AutoDuck")
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())