"""A single dark, modern QSS theme applied app-wide. Keeping it in its own
file so it doesn't clutter main.py and is easy to tweak/replace later."""

DARK_THEME = """
QMainWindow, QWidget {
    background-color: #1a1d23;
    color: #e6e6e6;
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 13px;
}

QSplitter::handle {
    background-color: #2a2e37;
    width: 2px;
}

/* --- Sidebar tree --- */
QTreeWidget {
    background-color: #20232b;
    border: 1px solid #2a2e37;
    border-radius: 6px;
    padding: 4px;
    outline: none;
}
QTreeWidget::item {
    padding: 6px 4px;
    border-radius: 4px;
}
QTreeWidget::item:selected {
    background-color: #2f6fed;
    color: white;
}
QTreeWidget::item:hover:!selected {
    background-color: #2a2e37;
}

/* --- Buttons --- */
QPushButton {
    background-color: #2f6fed;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 8px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #4b83f0;
}
QPushButton:pressed {
    background-color: #2559c9;
}
QPushButton:disabled {
    background-color: #3a3f4a;
    color: #8a8f99;
}

/* --- Inputs --- */
QLineEdit, QSpinBox, QComboBox {
    background-color: #20232b;
    border: 1px solid #3a3f4a;
    border-radius: 5px;
    padding: 6px 8px;
    color: #e6e6e6;
    selection-background-color: #2f6fed;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #2f6fed;
}
QComboBox QAbstractItemView {
    background-color: #20232b;
    color: #e6e6e6;
    selection-background-color: #2f6fed;
}

/* --- Labels --- */
QLabel {
    color: #e6e6e6;
}

/* --- Dialogs --- */
QDialog {
    background-color: #1a1d23;
}

/* --- Scrollbars --- */
QScrollBar:vertical {
    background: #1a1d23;
    width: 10px;
}
QScrollBar::handle:vertical {
    background: #3a3f4a;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #4b515e;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

/* --- Progress bar (scan dialog) --- */
QProgressBar {
    background-color: #20232b;
    border: 1px solid #3a3f4a;
    border-radius: 5px;
    text-align: center;
    color: #e6e6e6;
}
QProgressBar::chunk {
    background-color: #2f6fed;
    border-radius: 5px;
}

/* --- List widget (scan results) --- */
QListWidget {
    background-color: #20232b;
    border: 1px solid #3a3f4a;
    border-radius: 5px;
}
QListWidget::item {
    padding: 6px;
    border-bottom: 1px solid #2a2e37;
}
QListWidget::item:selected {
    background-color: #2f6fed;
    color: white;
}

/* --- Message boxes --- */
QMessageBox {
    background-color: #1a1d23;
}
"""
