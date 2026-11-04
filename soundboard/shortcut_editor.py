"""Record and edit the board's shortcuts without triggering sounds."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QLabel, QKeySequenceEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .hotkeys import ShortcutError


class ShortcutEditor(QDialog):
    def __init__(self, parent, actions, apply, selected=None):
        super().__init__(parent)
        self.setWindowTitle('Edit shortcuts')
        self.resize(650, 540)
        self.setMinimumSize(570, 380)
        self.apply = apply
        self.editors = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(16)
        heading = QLabel('Keyboard shortcuts')
        heading.setObjectName('title')
        layout.addWidget(heading)
        hint = QLabel('Click a shortcut field and press the new combination. Use Ctrl/Alt with a key, '
                      'an F-key (except F12), or a number-pad key with Num Lock on.\n'
                      'Clear removes a binding. Soundboard hotkeys are paused while this editor is open.')
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName('shortcutRows')
        grid = QGridLayout(content)
        grid.setColumnStretch(0, 1)
        grid.setVerticalSpacing(12)
        for row, (action, name, shortcut) in enumerate(actions):
            title = QLabel(name)
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setWordWrap(True)
            title.setMinimumWidth(100)
            title.setToolTip(name)
            grid.addWidget(title, row, 0)
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            edit.setMinimumWidth(195)
            edit.setAccessibleName(f'Shortcut for {name}')
            edit.setKeySequence(QKeySequence(shortcut) if isinstance(shortcut, str) else QKeySequence())
            grid.addWidget(edit, row, 1)
            clear = QPushButton('Clear')
            clear.setAutoDefault(False)
            clear.setAccessibleName(f'Clear shortcut for {name}')
            clear.clicked.connect(edit.clear)
            grid.addWidget(clear, row, 2)
            self.editors[action] = edit
        grid.setRowStretch(len(actions), 1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.error = QLabel()
        self.error.setObjectName('notice')
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        for btn in buttons.buttons():
            btn.setAutoDefault(False)
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName('primary')
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if selected in self.editors:
            edit = self.editors[selected]

            def focus_selected():
                scroll.ensureWidgetVisible(edit)
                edit.setFocus()
            QTimer.singleShot(0, focus_selected)

    def save(self):
        bindings = {action: edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                    for action, edit in self.editors.items()}
        try:
            self.apply(bindings)
        except ShortcutError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()
