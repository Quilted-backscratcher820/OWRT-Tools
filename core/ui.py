"""Small Qt table helpers shared by the desktop pages."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QHeaderView, QTableWidget
from shiboken6 import isValid


class AdaptiveColumnSizer(QObject):
    """Fit content columns and give the remaining viewport to one column."""

    def __init__(self, table: QTableWidget, stretch_column: int = 0, fixed_columns: tuple[int, ...] = ()) -> None:
        super().__init__(table)
        self.table = table
        self.stretch_column = stretch_column
        self.fixed_columns = fixed_columns
        self._pending = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionsMovable(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setWordWrap(False)
        self.viewport = self.table.viewport()
        self.viewport.installEventFilter(self)
        model = self.table.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.dataChanged, model.layoutChanged, model.modelReset):
            signal.connect(self.schedule)
        self.schedule()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.viewport and isValid(self.table) and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.schedule()
        return False

    def schedule(self, *_args: object) -> None:
        if isValid(self.table) and not self._pending:
            self._pending = True
            self._timer.start(0)

    def refresh(self) -> None:
        self._pending = False
        if not isValid(self.table) or self.table.columnCount() == 0:
            return
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        for column in self.fixed_columns:
            header_item = self.table.horizontalHeaderItem(column)
            header_width = self.table.fontMetrics().horizontalAdvance(header_item.text()) if header_item else 0
            self.table.setColumnWidth(column, max(56, header_width + 24, self.table.columnWidth(column)))
        # Leave a couple of pixels for the viewport frame/corner so a vertical
        # scrollbar appearing after a new row cannot create a 1px horizontal bar.
        available = max(0, self.table.viewport().width() - 2)
        other_width = sum(
            self.table.columnWidth(column)
            for column in range(self.table.columnCount())
            if column != self.stretch_column
        )
        minimum = max(80, header.minimumSectionSize())
        stretch = max(minimum, available - other_width)
        self.table.setColumnWidth(self.stretch_column, stretch)
