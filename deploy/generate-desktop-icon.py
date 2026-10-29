"""Build the Windows icon from the same approved icon used by the Qt window."""
import os
from pathlib import Path
import sys

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from time_logger.desktop_ui import application_icon

app = QApplication([])
target = Path(__file__).with_name('personal-time-logger.ico')
if not application_icon().pixmap(64, 64).save(str(target), 'ICO'):
    raise SystemExit('Application icon could not be saved')
print('application_icon_generated')
