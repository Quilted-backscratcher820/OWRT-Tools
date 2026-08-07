"""Small Qt table helpers shared by the desktop pages."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Slot
from PySide6.QtWidgets import QHeaderView, QTableWidget
from shiboken6 import isValid


class AdaptiveColumnSizer(QObject):
    """Fit content columns and give the remaining viewport to one column."""

    def __init__(self, table: QTableWidget, stretch_column: int = 0, fixed_columns: tuple[int, ...] = ()) -> None:
        # Keep the helper alive until the window is torn down.  Parenting it to
        # the top-level window lets a queued timeout outlive table destruction,
        # which avoids PySide6 reporting a missing slot.
        super().__init__(table.window())
        self.table = table
        self.stretch_column = stretch_column
        self.fixed_columns = fixed_columns
        self._disposed = False
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
        viewport = getattr(self, "viewport", None)
        table = getattr(self, "table", None)
        if (
            not getattr(self, "_disposed", True)
            and watched is viewport
            and table is not None
            and isValid(table)
            and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)
        ):
            self.schedule()
        return False

    @Slot()
    def schedule(self, *_args: object) -> None:
        table = getattr(self, "table", None)
        timer = getattr(self, "_timer", None)
        if (
            not getattr(self, "_disposed", True)
            and table is not None
            and timer is not None
            and isValid(table)
            and isValid(timer)
            and not self._pending
        ):
            self._pending = True
            timer.start(0)

    @Slot()
    def refresh(self) -> None:
        if getattr(self, "_disposed", True):
            return
        self._pending = False
        table = getattr(self, "table", None)
        if table is None or not isValid(table) or table.columnCount() == 0:
            return
        table.resizeColumnsToContents()
        header = table.horizontalHeader()
        for column in self.fixed_columns:
            header_item = table.horizontalHeaderItem(column)
            header_width = table.fontMetrics().horizontalAdvance(header_item.text()) if header_item else 0
            table.setColumnWidth(column, max(56, header_width + 24, table.columnWidth(column)))
        # Leave a couple of pixels for the viewport frame/corner so a vertical
        # scrollbar appearing after a new row cannot create a 1px horizontal bar.
        available = max(0, table.viewport().width() - 2)
        other_width = sum(
            table.columnWidth(column)
            for column in range(table.columnCount())
            if column != self.stretch_column
        )
        minimum = max(80, header.minimumSectionSize())
        stretch = max(minimum, available - other_width)
        table.setColumnWidth(self.stretch_column, stretch)

    def dispose(self) -> None:
        """Stop callbacks before the associated table is destroyed."""

        if getattr(self, "_disposed", True):
            return
        self._disposed = True
        timer = getattr(self, "_timer", None)
        if timer is not None and isValid(timer):
            timer.stop()
        viewport = getattr(self, "viewport", None)
        if viewport is not None and isValid(viewport):
            viewport.removeEventFilter(self)
