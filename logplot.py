# /// script
# dependencies = [
#   "pandas",
#   "pyarrow",
#   "PySide6",
#   "pyqtgraph",
#   "zstandard",
# ]
# ///

"""LogPlot - A Log CSV Plot Viewer
"""

import sys
import random
import os
import json
import time
import re
import urllib.request
from argparse import ArgumentParser
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QTreeView, QSplitter,
    QVBoxLayout, QColorDialog, QWidget, QMenuBar, QAbstractItemView, QMenu,
    QDialog, QFormLayout, QDoubleSpinBox, QPushButton, QLabel, QLineEdit, QMessageBox,
    QTableWidget, QTableWidgetItem, QHBoxLayout, QInputDialog
)
from PySide6.QtGui import QAction, QStandardItemModel, QStandardItem, QColor, QBrush, QActionGroup, QCursor
from PySide6.QtCore import Qt
import pyqtgraph as pg
from pyqtgraph import PlotWidget, DateAxisItem, PlotDataItem

VERSION = "20260903"


class SeriesState:
    """Holds all per-series runtime state: color, adjustment, plot items and labels."""
    def __init__(self, filepath, column_name, df: pd.DataFrame, timestamps, series_data = None):
        self.filepath = filepath
        self.df : pd.DataFrame = df
        self.column_name = column_name
        self.timestamps = timestamps
        self.series_data = series_data
        self.series_data_mapping = None

        self.display_name = self.column_name
        self.has_marker = False

        h = random.randint(0, 359)
        s = random.randint(150, 255)
        v = random.randint(200, 255)
        self.color = QColor.fromHsv(h, s, v) # QColor

        self.column_item = QStandardItem(self.display_name)
        self.column_item.setCheckable(True)
        self.column_item.setData(self)
        self.column_item.setForeground(QBrush(self.color))

        value_item = QStandardItem("")
        value_item.setEditable(False)
        self.value_item = value_item        # QStandardItem (tree live value)

        self.adjustment = (0.0, 1.0)        # (y_offset, y_scale)
        self.curve = None                   # PlotDataItem
        self.vb = None                      # ViewBox the curve lives in
        self.label = None                   # DraggableLabelItem
        self.value_label = None             # pg.TextItem (crosshair value)

    @property
    def is_active(self):
        """True when the curve has been added to the plot."""
        return self.curve is not None

    @property
    def is_string_type(self):
        if type(self.series_data) == np.ndarray:
            return False
        return self.series_data.dtype in ['object', 'str']

    def set_color(self, new_color):
        self.color = new_color

        self.column_item.setForeground(QBrush(self.color))

        if self.curve is not None:
            self.curve.setPen(color=self.color)
            if self.label:
                self.label.update_text(self.display_name, self.color)
            if self.value_label:
                self.value_label.setColor(self.color)

    def set_displayname(self, displayname):
        self.display_name = displayname
        self.column_item.setText(self.display_name)
        if self.label:
            self.label.update_text(self.display_name, self.color)

    def show_marker(self, show = True):
        self.has_marker = show
        if show:
            self.curve.setSymbol('x')
            self.curve.setSymbolSize(4)
            self.curve.setSymbolBrush(self.color)
            self.curve.setSymbolPen(self.color)
        else:
            self.curve.setSymbol(None)

    def clear(self):
        if self.vb:
            if self.curve:
                self.vb.removeItem(self.curve)
            if self.label:
                self.vb.removeItem(self.label)
            if self.value_label:
                self.vb.removeItem(self.value_label)
        self.curve = None
        self.vb = None
        self.label = None
        self.value_label = None

    def update_label_position(self):
        if self.label is None:
            return

        curve = self.curve
        if not hasattr(curve, 'y_min_val') or np.isnan(curve.y_min_val):
            self.label.setVisible(False)
            return

        # 將標籤維持在 Y 軸可視最大最小值的中間
        x_min, _ = self.vb.viewRange()[0]
        y_pos = (curve.y_min_val + curve.y_max_val) / 2.0

        self.label.setVisible(True)
        self.label.setPos(x_min, y_pos)

    def text_mapping(self):
        if not self.is_string_type:
            return

        # 建立字串到整數的映射
        unique_vals = self.series_data.dropna().unique()
        mapping = {val: i for i, val in enumerate(unique_vals)}
        curve_mapping = self.series_data.map(mapping)
        self.series_data_mapping = curve_mapping

    def search_by_timestamp(self, timestamp):
        """Searches for the value, numeric value (for plot position), and timestamp in this series.

        Returns:
            tuple: (val, numeric_val, closest_timestamp) or None if out of range or df is empty.
        """
        if self.df is None or self.df.empty:
            return None

        if timestamp < 1e11:
            timestamp_ns = int(timestamp * 1e9)
        else:
            timestamp_ns = int(timestamp)

        idx = self.df.index.searchsorted(timestamp_ns, side='right') - 1
        if idx < 0 or idx >= len(self.df):
            return None

        if isinstance(self.series_data, np.ndarray):
            val = self.series_data[idx]
            numeric_val = float(val)
        elif self.series_data_mapping is not None:
            val = self.series_data.iloc[idx]
            numeric_val = float(self.series_data_mapping.iloc[idx])
        elif self.series_data is not None:
            val = self.series_data.iloc[idx]
            try:
                numeric_val = float(val)
            except (ValueError, TypeError):
                numeric_val = 0.0
        else:
            val = self.df.iloc[idx][self.column_name]
            try:
                numeric_val = float(val)
            except (ValueError, TypeError):
                numeric_val = 0.0

        closest_timestamp = self.df.index[idx]
        return val, numeric_val, closest_timestamp


    def _create_curve(self, vb):
        if self.curve:
            return

        self.vb = vb
        self.curve = PlotDataItem(pen=pg.mkPen(color=self.color), paint=None)
        self.vb.addItem(self.curve)

        self.label = DraggableLabelItem(text=self.display_name, color=self.color, anchor=(-0.1, 0.5), ss=self)
        self.vb.addItem(self.label, ignoreBounds=True)

        self.value_label = pg.TextItem("", color=self.color, anchor=(-0.1, 0.5))
        bg_color = QColor('black')
        bg_color.setAlpha(150)
        self.value_label.fill = pg.mkBrush(bg_color)
        self.value_label.setVisible(False)
        self.vb.addItem(self.value_label, ignoreBounds=True)


    def _update_curve(self, show_marker):
        if self.curve is None:
            return

        # 建立時判斷是否為文字，是否可轉換為 numpy, 之後就不要一直重複判斷.

        if type(self.series_data) == np.ndarray:
            values = self.series_data
        elif self.series_data_mapping is not None:
            values = self.series_data_mapping.to_numpy(dtype=float)
        else:
            values = self.series_data.to_numpy(dtype=float)

        y_offset, y_scale = self.adjustment

        if np.isnan(values).all():
            base = 0.0
            min_val = np.nan
            max_val = np.nan
        else:
            base = np.nanmin(values)
            min_val = base
            max_val = np.nanmax(values)

        adjusted_values = (values - base) * y_scale + base + y_offset

        self.curve.setData(x=self.timestamps, y=adjusted_values)

        # 紀錄 Y 軸的最大與最小值，供 Label 定位在垂直置中時使用
        self.curve.y_min_val = (min_val - base) * y_scale + base + y_offset
        self.curve.y_max_val = (max_val - base) * y_scale + base + y_offset
        self.curve.base_val = base

        self.show_marker(show_marker)

    def update_curve_adjustment(self, y_offset, y_scale):
        if not self.is_active:
            return

        self.adjustment = (y_offset, y_scale)
        self._update_curve(self.has_marker)
        self.update_label_position()



class DraggableLabelItem(pg.TextItem):
    def __init__(self, text, color, anchor, ss: SeriesState):
        super().__init__(anchor=anchor)
        self.ss: SeriesState = ss
        self.setAcceptedMouseButtons(Qt.LeftButton | Qt.RightButton)
        self.is_dragging = False
        self.drag_mode = None # 'offset' or 'scale'
        self.initial_mouse_y = 0
        self.initial_offset = 0
        self.initial_scale = 1.0
        self.update_text(text, color)

    def _open_context_menu(self):
        view = self.getViewWidget()
        win = view.window() if view else None
        if not win and self.scene() and self.scene().views():
            win = self.scene().views()[0].window()
        if win and hasattr(win, 'show_series_context_menu'):
            win.show_series_context_menu(self.ss.column_item, QCursor.pos())

    def contextMenuEvent(self, ev):
        ev.accept()
        self._open_context_menu()

    def raiseContextMenu(self, ev):
        ev.accept()
        self._open_context_menu()

    def update_text(self, text, color):
        self.setText(text, color=color)
        self.border = pg.mkPen(color, width=1)
        self.fill = pg.mkBrush(0, 0, 0, 180)
        self.update()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            ev.accept()
            self.ss.column_item.setCheckState(Qt.Unchecked)
        else:
            ev.ignore()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            vb = self._viewBox()
            if vb is None:
                ev.ignore()
                return

            ev.accept()
            self.is_dragging = True

            if ev.modifiers() & Qt.ShiftModifier:
                self.drag_mode = 'scale'
            else:
                self.drag_mode = 'offset'

            # Map mouse position from scene to view coordinates
            view_pos = vb.mapSceneToView(ev.scenePos())
            self.initial_mouse_y = view_pos.y()
            self.initial_offset, self.initial_scale = self.ss.adjustment
        elif ev.button() == Qt.RightButton:
            ev.accept()
        else:
            ev.ignore()

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.RightButton:
            ev.accept()
            self._open_context_menu()

    def mouseMoveEvent(self, ev):
        if self.is_dragging:
            vb = self._viewBox()
            if vb is None:
                ev.ignore()
                return

            ev.accept()
            # Map mouse position from scene to view coordinates
            view_pos = vb.mapSceneToView(ev.scenePos())
            current_mouse_y = view_pos.y()
            dy = current_mouse_y - self.initial_mouse_y

            if self.drag_mode == 'offset':
                new_offset = self.initial_offset + dy
                self.ss.update_curve_adjustment(new_offset, self.initial_scale)
            elif self.drag_mode == 'scale':
                view_range_y = vb.viewRange()[1]
                view_height = view_range_y[1] - view_range_y[0]
                if view_height == 0: return

                # Exponential scaling feels more natural
                scale_factor = np.exp(dy / (view_height / 2.0))
                new_scale = self.initial_scale * scale_factor
                new_scale = max(0.001, new_scale) # Clamp to a minimum value
                self.ss.update_curve_adjustment(self.initial_offset, new_scale)
        else:
            ev.ignore()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.is_dragging:
            ev.accept()
            self.is_dragging = False
            self.drag_mode = None
        else:
            ev.ignore()



def getOffsetFromUtc():
    """Retrieve the utc offset respecting the daylight saving time"""
    ts = time.localtime()
    if ts.tm_isdst:
        utc_offset = time.altzone
    else:
        utc_offset = time.timezone
    return utc_offset

class CSVPlotViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"LogPlot")
        self.current_timestamp = None
        self.keyboard_mode = False
        self.measure_mode = False
        self.show_markers = False
        self.show_values_mode = True
        self.in_search_mode = False # 新增：用於區分表格顯示模式

        # 初始化資料結構
        self.dataframes = {}    # file_path -> DataFrame
        self.series = {}        # (file_path, column_name) -> SeriesState
        self.file_items = {}    # file_path -> QStandardItem (for tracking tree nodes)
        self.active_text_series = set() # 用於追蹤已勾選的字串系列

        # 初始化搜索功能相關變數
        self.search_results = []
        self.raw_search_matches = []
        self.pinned_results = {} # (file_path, timestamp, series) -> dict of pinned info
        self.current_search_index = -1

        self._init_ui()
        self._create_menu()

        # 勾選變更事件處理
        self.model.itemChanged.connect(self.on_item_changed)

    def _init_ui(self):
        # 中央分割視窗 (垂直分割)
        self.main_splitter = QSplitter(Qt.Vertical)
        self.setCentralWidget(self.main_splitter)

        # 上半部 (原有的水平分割視窗)
        self.top_widget = QWidget()
        top_layout = QVBoxLayout(self.top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Horizontal)
        top_layout.addWidget(self.splitter)
        self.main_splitter.addWidget(self.top_widget)

        # 左側 pyqtgraph 繪圖區
        self.x_axis = DateAxisItem(orientation='bottom', utcOffset=getOffsetFromUtc())
        self.plot_widget = PlotWidget(axisItems={'bottom': self.x_axis})
        self.plot_widget.showGrid(x=True, y=True)
        # self.plot_widget.addLegend()
        self.splitter.addWidget(self.plot_widget)

        # Enable zooming and panning with the mouse
        self.plot_widget.setMouseTracking(True)
        self.plot_widget.setAcceptDrops(True)
        self.plot_widget.wheelEvent = self.wheel_zoom

        # Enable Drop event
        self.setAcceptDrops(True)

        self.main_vb = self.plot_widget.plotItem.getViewBox()
        # self.main_vb.setMenuEnabled(False)
        for action in self.main_vb.menu.actions():
            if "View All" not in action.text():
                action.setVisible(False)

        self.plot_widget.showAxis('right')
        self.second_vb = pg.ViewBox()
        self.plot_widget.scene().addItem(self.second_vb)
        self.plot_widget.getAxis('right').linkToView(self.second_vb)
        self.second_vb.setXLink(self.main_vb)
        # self.second_vb.setMenuEnabled(False)
        for action in self.second_vb.menu.actions():
            if "View All" not in action.text():
                action.setVisible(False)

        self.mouse_vb = pg.ViewBox()
        self.plot_widget.scene().addItem(self.mouse_vb)
        self.mouse_vb.setXLink(self.main_vb)
        self.mouse_vb.setYLink(self.main_vb)
        self.mouse_vb.setMenuEnabled(False)

        # Vertical line setup
        self.v_line = pg.InfiniteLine(angle=90, movable=False)
        self.mouse_vb.addItem(self.v_line, ignoreBounds=True)
        self.v_line.setVisible(True)
        self.x_axis_label = pg.TextItem(anchor=(0.5, 1))
        self.mouse_vb.addItem(self.x_axis_label, ignoreBounds=True)

        self.plot_widget.scene().sigMouseMoved.connect(self.mouse_moved)

        self.main_vb.sigRangeChanged.connect(self.update_label_positions)
        self.main_vb.sigResized.connect(self.update_viewbox_geometry)
        self.update_viewbox_geometry()


        # 右側 QTreeView 控制面板
        self.right_panel_widget = QWidget()
        self.right_panel_layout = QVBoxLayout(self.right_panel_widget)
        self.splitter.addWidget(self.right_panel_widget)

        # Filter input
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter series...")
        self.filter_edit.textChanged.connect(self.filter_tree_view)
        self.right_panel_layout.addWidget(self.filter_edit)

        self.tree_view = QTreeView()
        self.tree_view.setIndentation(10)
        self.tree_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Data Series", "Value"])
        self.tree_view.setModel(self.model)
        self.tree_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self.show_context_menu)
        self.right_panel_layout.addWidget(self.tree_view)

        # 下半部：包含搜索功能和文字數據表格的佈局
        self.bottom_widget = QWidget()
        self.bottom_layout = QVBoxLayout(self.bottom_widget)
        self.bottom_layout.setContentsMargins(5, 5, 5, 5)
        self.main_splitter.addWidget(self.bottom_widget)

        # 搜索控制項
        self.search_widget = QWidget()
        self.search_layout = QHBoxLayout(self.search_widget)
        self.search_layout.setContentsMargins(0, 0, 0, 0)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("在所有已選文字系列中搜索 (Enter)...")
        self.search_input.returnPressed.connect(self.search_global_text)
        self.search_input.textChanged.connect(self.clear_search_if_empty)

        self.search_prev_button = QPushButton("上一筆 (Shift+F3)")
        self.search_next_button = QPushButton("下一筆 (F3)")
        self.search_status_label = QLabel("")
        self.search_status_label.setMinimumWidth(80)

        self.search_prev_button.clicked.connect(self.find_previous_result)
        self.search_next_button.clicked.connect(self.find_next_result)

        self.search_layout.addWidget(QLabel("全局搜索:"))
        self.search_layout.addWidget(self.search_input, 1)
        self.search_layout.addWidget(self.search_prev_button)
        self.search_layout.addWidget(self.search_next_button)
        self.search_layout.addWidget(self.search_status_label)

        self.bottom_layout.addWidget(self.search_widget)

        # 文字數據表格
        self.text_data_table = QTableWidget()
        self.text_data_table.setColumnCount(4)
        self.text_data_table.setHorizontalHeaderLabels(["Timestamp", "File", "Series", "Value"])
        self.text_data_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.text_data_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.text_data_table.horizontalHeader().setStretchLastSection(True)
        self.text_data_table.setColumnWidth(0, 180)
        self.text_data_table.setColumnWidth(1, 150)
        self.text_data_table.setColumnWidth(2, 150)
        self.text_data_table.setMouseTracking(True)
        self.text_data_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.text_data_table.customContextMenuRequested.connect(self.show_table_context_menu)
        self.text_data_table.itemDoubleClicked.connect(self.jump_to_result_from_double_click)
        self.text_data_table.cellEntered.connect(self.jump_to_result_from_hover)
        self.bottom_layout.addWidget(self.text_data_table)
        self.bottom_widget.setVisible(True) # 初始時預設顯示下半部

        # 調整分割器比例 (縮小下方訊息視窗預設高度)
        self.main_splitter.setSizes([850, 150])

        # Measurement mode
        pen = pg.mkPen('y', style=Qt.DashLine)
        self.measure_line1 = pg.InfiniteLine(angle=90, movable=True, pen=pen)
        self.measure_line2 = pg.InfiniteLine(angle=90, movable=True, pen=pen)
        self.measure_label = pg.TextItem(anchor=(0.5, 2), color='y')
        self.measure_text1 = pg.TextItem(anchor=(1, 1.5), color='y')
        self.measure_text2 = pg.TextItem(anchor=(0, 1.5), color='y')

        self.measure_line1.setVisible(False)
        self.measure_line2.setVisible(False)
        self.measure_label.setVisible(False)
        self.measure_text1.setVisible(False)
        self.measure_text2.setVisible(False)

        self.mouse_vb.addItem(self.measure_line1, ignoreBounds=True)
        self.mouse_vb.addItem(self.measure_line2, ignoreBounds=True)
        self.mouse_vb.addItem(self.measure_label, ignoreBounds=True)
        self.mouse_vb.addItem(self.measure_text1, ignoreBounds=True)
        self.mouse_vb.addItem(self.measure_text2, ignoreBounds=True)

        self.measure_line1.sigPositionChanged.connect(self.update_measure_label)
        self.measure_line2.sigPositionChanged.connect(self.update_measure_label)
        self.main_vb.sigRangeChanged.connect(self.update_measure_label)

    # def uncheck_series(self, ss: SeriesState):
    #     """Finds an item in the tree model by its key and unchecks it."""
    #     ss: SeriesState = self.series.get(key_to_uncheck)
    #     ss.child_item.setCheckState(Qt.Unchecked)

    #     # for row in range(self.model.rowCount()):
    #     #     file_item = self.model.item(row)
    #     #     if not file_item:
    #     #         continue

    #     #     # Check if the file path in the key matches the file item's path
    #     #     if file_item.data() == key_to_uncheck[0]:
    #     #         for child_row in range(file_item.rowCount()):
    #     #             child_item = file_item.child(child_row)
    #     #             if child_item and child_item.data() == key_to_uncheck:
    #     #                 child_item.setCheckState(Qt.Unchecked)
    #     #                 return # Found and unchecked, so we can exit


    def _create_menu(self):
        menubar = QMenuBar(self)
        self.setMenuBar(menubar)

        file_menu = menubar.addMenu("File")
        open_action = QAction("Open CSV Files", self)
        open_action.triggered.connect(self.select_csv_files)
        file_menu.addAction(open_action)

        save_state_action = QAction("Save State", self)
        save_state_action.triggered.connect(self.save_state)
        file_menu.addAction(save_state_action)

        load_state_action = QAction("Load State", self)
        load_state_action.triggered.connect(self.load_state)
        file_menu.addAction(load_state_action)

        mode_menu = menubar.addMenu("Mode")

        self.keyboard_mode_action = QAction("Keyboard Navigation Mode (Space)", self)
        self.keyboard_mode_action.setCheckable(True)
        self.keyboard_mode_action.setChecked(self.keyboard_mode)
        self.keyboard_mode_action.triggered.connect(self.toggle_keyboard_mode)
        mode_menu.addAction(self.keyboard_mode_action)

        self.measure_mode_action = QAction("Measurement Mode (F2)", self)
        self.measure_mode_action.setCheckable(True)
        self.measure_mode_action.setChecked(self.measure_mode)
        self.measure_mode_action.triggered.connect(self.toggle_measure_mode)
        mode_menu.addAction(self.measure_mode_action)

        self.show_values_action = QAction("Show Curve Values (S)", self)
        self.show_values_action.setCheckable(True)
        self.show_values_action.setChecked(self.show_values_mode)
        self.show_values_action.triggered.connect(self.toggle_show_values)
        mode_menu.addAction(self.show_values_action)

        self.show_markers_action = QAction("Show Data Markers (M)", self)
        self.show_markers_action.setCheckable(True)
        self.show_markers_action.setChecked(self.show_markers)
        self.show_markers_action.triggered.connect(self.toggle_show_markers)
        mode_menu.addAction(self.show_markers_action)

        mode_menu.addSeparator()

        self.bottom_panel_action = QAction("Show Bottom Panel (F4)", self)
        self.bottom_panel_action.setCheckable(True)
        self.bottom_panel_action.setChecked(not self.bottom_widget.isHidden())
        self.bottom_panel_action.triggered.connect(self.toggle_bottom_widget)
        mode_menu.addAction(self.bottom_panel_action)

        tz_menu = menubar.addMenu("Timezone")
        tz_group = QActionGroup(self)
        for i in range(12):
            act = tz_group.addAction(f"UTC-{12-i}")
            act.setCheckable(True)
            act.setData(-(12-i))
            tz_menu.addAction(act)
        act = tz_group.addAction(f"UTC")
        act.setCheckable(True)
        act.setData(0)
        tz_menu.addAction(act)
        for i in range(12):
            act = tz_group.addAction(f"UTC+{i}")
            act.setCheckable(True)
            act.setData(i)
            tz_menu.addAction(act)

        tz_group.setExclusive(True)
        tz_group.triggered.connect(lambda: self.set_timezone_offset(tz_group.checkedAction().data()))
        ofst = int(-self.x_axis.utcOffset / 3600) + 13
        tz_group.actions()[ofst].setChecked(True)

        help_menu = menubar.addMenu("Help")
        usage_action = QAction("Usage", self)
        usage_action.triggered.connect(self.show_usage_dialog)
        help_menu.addAction(usage_action)

        update_action = QAction("Update", self)
        update_action.triggered.connect(self.update_script)
        help_menu.addAction(update_action)


    def show_usage_dialog(self):
        help_text = f"""
<h2>LogPlot 使用說明</h2>
<p>版本: {VERSION}</p>
<h3>滑鼠操作:</h3>
<ul>
    <li><b>左鍵拖曳:</b> 平移視圖。</li>
    <li><b>滾輪:</b> 水平捲動。</li>
    <li><b>Ctrl + 滾輪:</b> 縮放視圖。</li>
    <li><b>懸停:</b> 顯示十字線及當前時間戳的數值。</li>
</ul>
<h3>曲線標籤操作:</h3>
<ul>
    <li><b>左鍵拖曳:</b> 調整曲線的 Y 軸偏移。</li>
    <li><b>Shift + 左鍵拖曳:</b> 調整曲線的 Y 軸縮放。</li>
    <li><b>左鍵雙擊:</b> 取消勾選 (隱藏) 該曲線。</li>
</ul>
<h3>鍵盤操作:</h3>
<ul>
    <li><b>空白鍵:</b> 切換鍵盤導覽模式。此模式下十字線不會跟隨滑鼠。</li>
    <li><b>左右方向鍵:</b> 移動十字線到上一個/下一個資料點。</li>
    <li><b>F1:</b> 顯示此說明視窗。</li>
    <li><b>F2:</b> 切換測量模式。出現兩條垂直線以測量時間差。</li>
    <li><b>F3:</b> 尋找下一個搜尋結果。</li>
    <li><b>Shift+F3:</b> 尋找上一個搜尋結果。</li>
    <li><b>F4:</b> 切換顯示/隱藏下方訊息視窗。</li>
    <li><b>M:</b> 切換顯示曲線的資料點標記 (Marker)。</li>
    <li><b>S:</b> 切換顯示曲線數值，在滑鼠線上和各線段交點處顯示標註數值。</li>
</ul>
<h3>檔案與選單操作:</h3>
<ul>
    <li>將 CSV 檔案拖放到視窗中以載入。</li>
    <li><b>File -> Open CSV Files:</b> 選擇並載入 CSV 檔案。</li>
    <li><b>File -> Save State:</b> 儲存目前的狀態 (載入的檔案、勾選的曲線、顏色、縮放等) 到檔案。</li>
    <li><b>File -> Load State:</b> 載入先前儲存的狀態。</li>
    <li><b>Timezone:</b> 切換時區，以不同 UTC 偏移量顯示時間。</li>
    <li><b>Help -> Update:</b> 從網路上下載並更新程式至最新版本。</li>
</ul>
<h3>樹狀圖與清單操作:</h3>
<ul>
    <li><b>過濾框 (Filter):</b> 輸入文字過濾顯示的資料列。</li>
    <li><b>右鍵選單 (檔案層級):</b> 載入該檔案的訊號描述檔 (SignalList)、清除該檔案所有選取、移除檔案。</li>
    <li><b>右鍵選單 (資料列層級):</b> 修改名稱 (Rename Series)、更改曲線顏色、手動調整 Y 軸偏移與縮放、產生一次微分 (_1nd)、二次微分 (_2nd) 及絕對值 (_abs) 資料。</li>
    <li><b>右鍵選單 (通用):</b> 展開/折疊全部、勾選/取消勾選所有過濾結果、移除所有檔案。</li>
</ul>
<h3>搜尋與文字資料功能:</h3>
<ul>
    <li>在右側樹狀圖中勾選要搜尋的文字序列。</li>
    <li>在下方的搜尋框中輸入文字並按 Enter 鍵。</li>
    <li>搜尋結果會顯示在下方表格。雙擊結果可跳轉至對應時間點。</li>
</ul>
        """
        QMessageBox.about(self, "LogPlot Usage", help_text)

    def update_script(self):
        """Downloads the latest version of the script and replaces the current one."""
        update_url = "https://raw.githubusercontent.com/wctang/logv/refs/heads/main/logplot.py"

        try:
            self.statusBar().showMessage("Checking for updates...")
            QApplication.processEvents()

            req_url = f"{update_url}?t={int(time.time())}"
            req = urllib.request.Request(req_url, headers={
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            })
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.getcode() != 200:
                    raise Exception(f"Failed to download. Status code: {response.getcode()}")

                new_script_content_bytes = response.read()
                new_script_content = new_script_content_bytes.decode('utf-8', errors='ignore')

                match = re.search(r'VERSION\s*=\s*["\'](\d+)["\']', new_script_content)
                if not match:
                    raise Exception("Could not find version in the new script.")

                online_version = match.group(1)

                if online_version == VERSION:
                    QMessageBox.information(self, "No Update Needed", f"You are already using the latest version ({VERSION}).")
                    return

                reply = QMessageBox.question(self, "Update Available",
                                             f"A new version ({online_version}) is available. Your current version is {VERSION}.\n\n"
                                             f"This will download the latest version from:\n{update_url}\n\n"
                                             "And replace the current script. Are you sure you want to continue?",
                                             QMessageBox.Yes | QMessageBox.No, QMessageBox.No)

                if reply == QMessageBox.No:
                    return

                self.statusBar().showMessage("Updating...")
                QApplication.processEvents()

                script_path = os.path.abspath(sys.argv[0])

                if not script_path.lower().endswith('.py'):
                    QMessageBox.warning(self, "Update Not Supported", "Automatic update is only supported when running as a .py script.")
                    return

                timestamp_str = datetime.now().strftime('%Y%m%d-%H%M%S')
                backup_path = f"{script_path}.{timestamp_str}.bak"
                try:
                    os.rename(script_path, backup_path)
                except OSError as e:
                    raise OSError(f"Could not back up current script: {e}")

                try:
                    with open(script_path, 'wb') as f:
                        f.write(new_script_content_bytes)
                    QMessageBox.information(self, "Update Complete", f"Update successful! Old version backed up to:\n{backup_path}\n\nPlease restart the application.")
                except OSError as e:
                    QMessageBox.critical(self, "Update Failed", f"An error occurred while writing the new file: {e}\nAttempting to restore from backup.")
                    os.rename(backup_path, script_path)

        except Exception as e:
            QMessageBox.critical(self, "Update Failed", f"An error occurred during the update:\n{e}")
        finally:
            self.statusBar().clearMessage()

    def set_timezone_offset(self, offset):
        self.x_axis.utcOffset = -offset * 3600
        self.x_axis.picture = None
        self.x_axis.update()
        self.plot_widget.update()


    def select_csv_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(self, "Open CSV Files", "", "CSV Files (*.csv *.csv.zst *.csv.zstd)")
        if file_paths:
            self.load_csv_files(file_paths)

    def select_signallist_for_file(self, file_item):
        file_paths, _ = QFileDialog.getOpenFileNames(self, "Load SignalList Files", "", "CSV Files (*.csv)")
        if file_paths:
            self.load_signallist_files(file_paths, file_item)

    def save_state(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Save State", "", "State Files (*.state)")
        if not file_path:
            return

        state = {
            "files": list(self.dataframes.keys()),
            "series": [],
            "main_vb_range": self.main_vb.viewRange(),
            "second_vb_range": self.second_vb.viewRange()
        }

        for row in range(self.model.rowCount()):
            file_item = self.model.item(row)
            if not file_item:
                continue
            fpath = file_item.data()
            for child_row in range(file_item.rowCount()):
                child_item = file_item.child(child_row)
                if not child_item:
                    continue
                if child_item.checkState() == Qt.Checked:
                    s: SeriesState = child_item.data()
                    # col_name = key[1]
                    # s = self.series.get(key)
                    color = s.color.name() if s else QColor(Qt.white).name()
                    offset, scale = s.adjustment if s else (0.0, 1.0)
                    state["series"].append({
                        "file_path": fpath,
                        "column_name": s.column_name,
                        "display_name": s.display_name,
                        "color": color,
                        "y_offset": offset,
                        "y_scale": scale
                    })

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save state:\n{e}")

    def load_state(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Load State", "", "State Files (*.state)")
        if not file_path:
            return

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load state:\n{e}")
            return

        self.remove_all_files()

        files_to_load = [f for f in state.get("files", []) if os.path.exists(f)]
        if files_to_load:
            self.load_csv_files(files_to_load)

        for entry in state.get("series", []):
            key = (entry["file_path"], entry["column_name"])
            ss = self.series.get(key)
            if ss:
                if "display_name" in entry:
                    ss.set_displayname(entry["display_name"])
                ss.color = QColor(entry.get("color", "#FFFFFF"))
                ss.adjustment = (entry.get("y_offset", 0.0), entry.get("y_scale", 1.0))

            for row in range(self.model.rowCount()):
                file_item = self.model.item(row)
                if file_item and file_item.data() == entry["file_path"]:
                    for child_row in range(file_item.rowCount()):
                        child_item = file_item.child(child_row)
                        if child_item and child_item.data() == ss:
                            # color = self.series[key].color if key in self.series else QColor(Qt.white)
                            child_item.setForeground(QBrush(ss.color))
                            child_item.setCheckState(Qt.Checked)
                            break
                    break

        main_range = state.get("main_vb_range")
        if main_range:
            self.main_vb.setRange(xRange=main_range[0], yRange=main_range[1], padding=0)

        second_range = state.get("second_vb_range")
        if second_range:
            self.second_vb.setRange(xRange=second_range[0], yRange=second_range[1], padding=0)

    def load_csv_files(self, file_paths):
        for path in file_paths:
            try:
                comp = 'zstd' if path.lower().endswith(('.zst', '.zstd')) else 'infer'
                try:
                    df = pd.read_csv(path, engine="pyarrow", compression=comp)
                except Exception:
                    df = pd.read_csv(path, low_memory=False, keep_default_na=True, engine="c", compression=comp)

                if df.empty or df.shape[1] < 2:
                    continue

                if df.columns[0] != "timestamp":
                    df.rename(columns={df.columns[0]: 'timestamp'}, inplace=True)

                if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
                    try:
                        # try to detect unit, time must in 1970 ~ 2286
                        if df['timestamp'][0] < 1e11:
                            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                        elif df['timestamp'][0] < 1e14:
                            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        elif df['timestamp'][0] < 1e17:
                            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='us')
                        else:
                            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ns')

                    except Exception:
                        parsed = False
                        formats = ['%Y-%m-%d %H:%M:%S.%f', '%Y/%m/%d %H:%M', '%Y-%m-%d %H:%M:%S', 'ISO8601']
                        for fmt in formats:
                            try:
                                df['timestamp'] = pd.to_datetime(df['timestamp'], format=fmt)
                                parsed = True
                                break
                            except ValueError:
                                continue
                        if not parsed:
                            df['timestamp'] = pd.to_datetime(df['timestamp'])

                # to datetime64 ns with int64
                df['timestamp'] = df['timestamp'].astype('datetime64[ns]').astype(np.int64)

                df.set_index('timestamp', inplace=True)
                if not df.index.is_monotonic_increasing:
                    df.sort_index(inplace=True)

                # Handle duplicate column names by renaming them (e.g., 'col' -> 'col.0', 'col.1')
                cols = pd.Series(df.columns)
                if cols.duplicated().any():
                    for dup in cols[cols.duplicated()].unique():
                        dup_indices = cols[cols == dup].index.values
                        cols[dup_indices] = [f"{dup}.{i}" for i in range(len(dup_indices))]
                    df.columns = cols

                self.dataframes[path] = df

                self._populate_tree(path, df)
            except Exception as e:
                print(f"Error reading {path}: {e}")

        if file_paths and self.dataframes:
            global_xmin = min(df.index.min() for df in self.dataframes.values())
            global_xmax = max(df.index.max() for df in self.dataframes.values())
            self.plot_widget.setXRange(global_xmin / 1e9, global_xmax / 1e9, padding=0.05)

    def load_signallist_files(self, file_paths, file_item):
        signallist = {}
        for path in file_paths:
            try:
                df = pd.read_csv(path, low_memory=False, keep_default_na=False)
                if df.empty or df.shape[1] < 2:
                    continue

                for index, row in df.iterrows():
                    key = str(row.iloc[0]).strip()
                    if key:
                        signallist[key] = str(row.iloc[1]).strip()

            except Exception as e:
                print(f"Error reading {path}: {e}")

        if not file_item:
            return

        for child_row in range(file_item.rowCount()):
            child_item = file_item.child(child_row)
            if not child_item:
                continue

            ss: SeriesState = child_item.data()
            if ss and ss.column_name in signallist:
                ss.set_displayname(ss.column_name + ": " + signallist[ss.column_name])

    def _populate_tree(self, file_path, df):
        filename = os.path.basename(file_path)
        file_item = QStandardItem(filename)
        file_item.setCheckable(False)
        file_item.setData(file_path) # 儲存完整路徑以便後續操作
        self.file_items[file_path] = file_item

        timestamps = df.index.astype(np.int64) / 1e9

        for col in df.columns:
            key = (file_path, col)
            if key not in self.series:
                self.series[key] = SeriesState(file_path, col, df, timestamps, df[col])

            file_item.appendRow([self.series[key].column_item, self.series[key].value_item])

        # Add to model after all children are appended to avoid multiple UI refresh signals
        self.model.appendRow(file_item)

    def _find_none_overlap_offset(self):
        y_max_values = [ss.curve.y_max_val for ss in self.series.values()
                        if ss.is_active and ss.vb == self.second_vb
                        and hasattr(ss.curve, 'y_max_val') and not np.isnan(ss.curve.y_max_val)]
        new_offset = max(y_max_values) + 1.5 if y_max_values else 0.0
        return (new_offset, 1.0)

    def _update_vb_yrange(self, vb):
        if vb.state['autoRange'][1]:
            y_bounds = vb.childrenBounds()[1]
            if y_bounds:
                vb.setYRange(y_bounds[0], y_bounds[1], padding=0.05)
            else:
                vb.setYRange(0, 1)
            vb.state['autoRange'][1] = True

    def on_item_changed(self, item):
        if not item.isCheckable() or not item.data():
            return

        ss: SeriesState = item.data()
        df = self.dataframes[ss.filepath]
        key = (ss.filepath, ss.column_name)

        if item.checkState() == Qt.Checked:
            if ss.is_active:
                return

            if ss.is_string_type:
                # 檢查獨立字串數量，如果小於15，則當作分類數據繪圖
                unique_vals = df[ss.column_name].dropna().unique()
                if len(unique_vals) >= 15:
                    self.active_text_series.add(key)
                    self.update_crosshair(self.current_timestamp) # Refresh table for live mode
                    return

                # 建立字串到整數的映射
                ss.text_mapping()

                # 為新的分類曲線計算一個不重疊的 Y 軸偏移
                if ss.adjustment == (0.0, 1.0):
                    ss.adjustment = self._find_none_overlap_offset()

                vb = self.second_vb   # 在右邊的 ViewBox 繪製
            else:
                values = df[ss.column_name].to_numpy(dtype=float)
                unique_vals = np.unique(values[~np.isnan(values)])
                if len(unique_vals) < 3:
                    if ss.adjustment == (0.0, 1.0):
                        ss.adjustment = self._find_none_overlap_offset()

                    vb = self.second_vb
                else:
                    vb = self.main_vb

            # 建立曲線
            ss._create_curve(vb)
            ss._update_curve(self.show_markers)
            self.update_label_positions()
            self._update_vb_yrange(vb)

        elif item.checkState() == Qt.Unchecked:
            if ss.is_string_type:
                if ss.is_active:
                    vb = ss.vb
                    ss.clear()
                    self._update_vb_yrange(vb)
                elif key in self.active_text_series:
                    self.active_text_series.remove(key)
                    # If in search mode, re-run the search. Otherwise, update live view.
                    if self.in_search_mode:
                        self.search_global_text()
                    else:
                        self.update_crosshair(self.current_timestamp) # Refresh table for live mode
            else:
                if ss.is_active:
                    vb = ss.vb
                    ss.clear()
                    self._update_vb_yrange(vb)

    def update_label_positions(self):
        x_min, _ = self.main_vb.viewRange()[0]

        for ss in self.series.values():
            if not ss.is_active:
                continue

            ss.update_label_position()


    def _add_common_tree_actions(self, menu):
        expand_all_action = QAction("Expand All", self)
        expand_all_action.triggered.connect(self.tree_view.expandAll)
        menu.addAction(expand_all_action)

        collapse_all_action = QAction("Collapse All", self)
        collapse_all_action.triggered.connect(self.tree_view.collapseAll)
        menu.addAction(collapse_all_action)

        menu.addSeparator()

        check_filtered_action = QAction("Check All Filtered", self)
        check_filtered_action.triggered.connect(lambda: self.set_filtered_items_check_state(Qt.Checked))
        menu.addAction(check_filtered_action)

        uncheck_filtered_action = QAction("Uncheck All Filtered", self)
        uncheck_filtered_action.triggered.connect(lambda: self.set_filtered_items_check_state(Qt.Unchecked))
        menu.addAction(uncheck_filtered_action)

        menu.addSeparator()

        clear_marks_action = QAction("Clear All Markers (清除所有標記)", self)
        clear_marks_action.triggered.connect(self.clear_all_pinned_results)
        clear_marks_action.setEnabled(bool(self.pinned_results))
        menu.addAction(clear_marks_action)

        menu.addSeparator()

        remove_all_action = QAction("Remove All Files", self)
        remove_all_action.triggered.connect(self.remove_all_files)
        menu.addAction(remove_all_action)

    def show_series_context_menu(self, item, global_pos):
        if item is None:
            return
        if item.column() != 0 and item.parent():
            item = item.parent().child(item.row(), 0)

        menu = QMenu(self)

        rename_action = QAction("Rename Series", self)
        rename_action.triggered.connect(lambda: self.rename_series(item))
        menu.addAction(rename_action)

        change_color_action = QAction("Change Color", self)
        change_color_action.triggered.connect(lambda: self.change_column_color(item))
        menu.addAction(change_color_action)

        adjust_curve_action = QAction("Adjust Y Position and Scale", self)
        adjust_curve_action.triggered.connect(lambda: self.adjust_curve_position_scale(item))
        menu.addAction(adjust_curve_action)

        menu.addSeparator()

        diff1_action = QAction("1st Derivative", self)
        diff1_action.triggered.connect(lambda: self.create_computed_series(item, 1))
        menu.addAction(diff1_action)

        diff2_action = QAction("2nd Derivative", self)
        diff2_action.triggered.connect(lambda: self.create_computed_series(item, 2))
        menu.addAction(diff2_action)

        abs_action = QAction("Absolute Value", self)
        abs_action.triggered.connect(lambda: self.create_computed_series(item, "abs"))
        menu.addAction(abs_action)

        menu.addSeparator()

        self._add_common_tree_actions(menu)

        menu.exec(global_pos)

    def show_context_menu(self, position):
        index = self.tree_view.indexAt(position)
        global_pos = self.tree_view.viewport().mapToGlobal(position)

        if index.isValid():
            item = self.model.itemFromIndex(index)
            if item.parent() is not None:
                self.show_series_context_menu(item, global_pos)
                return

            menu = QMenu(self)

            load_signal_action = QAction("Load SignalList Files", self)
            load_signal_action.triggered.connect(
                lambda checked=False, file_item=item: self.select_signallist_for_file(file_item)
            )
            menu.addAction(load_signal_action)

            remove_file_action = QAction("Remove File", self)
            remove_file_action.triggered.connect(
                lambda checked=False, file_item=item: self.remove_file(file_item)
            )
            menu.addAction(remove_file_action)

            menu.addSeparator()
        else:
            menu = QMenu(self)

        self._add_common_tree_actions(menu)

        menu.exec(global_pos)

    def set_filtered_items_check_state(self, state):
        for row in range(self.model.rowCount()):
            file_item = self.model.item(row)
            if file_item and not self.tree_view.isRowHidden(row, self.model.invisibleRootItem().index()):
                for child_row in range(file_item.rowCount()):
                    column_item = file_item.child(child_row)
                    if column_item and column_item.isCheckable():
                        if not self.tree_view.isRowHidden(child_row, file_item.index()):
                            column_item.setCheckState(state)

    def remove_all_files(self):
        """從應用程式中移除所有檔案及其相關資料"""
        # 反向迭代以防在刪除時影響 index
        for row in range(self.model.rowCount() - 1, -1, -1):
            file_item = self.model.item(row)
            if file_item:
                self.remove_file(file_item)

    def remove_file(self, file_item):
        """從應用程式中移除一個檔案及其所有相關資料"""
        file_path = file_item.data()
        if not file_path:
            return

        # 1. 移除所有相關的曲線、標籤並清理 series 資料
        keys_to_remove = [key for key in self.series if key[0] == file_path]
        for key in keys_to_remove:
            ss = self.series.pop(key)
            if ss.is_active:
                ss.vb.removeItem(ss.curve)
                if ss.label:
                    ss.vb.removeItem(ss.label)
                if ss.value_label:
                    ss.vb.removeItem(ss.value_label)
        self.file_items.pop(file_path, None)

        # 3. 清理已勾選的文字序列
        series_to_remove = {s for s in self.active_text_series if s[0] == file_path}
        self.active_text_series -= series_to_remove

        # 清理該檔案相關的標記
        keys_to_remove = [k for k in self.pinned_results if k[0] == file_path]
        for k in keys_to_remove:
            info = self.pinned_results.pop(k)
            if info.get('line'):
                try:
                    self.mouse_vb.removeItem(info['line'])
                except Exception:
                    pass

        # 4. 移除 DataFrame
        self.dataframes.pop(file_path, None)

        # 5. 從 TreeView 模型中移除項目
        self.model.removeRow(file_item.row())

        # 6. 如果在搜索模式下，刷新搜索結果
        if self.in_search_mode:
            self.search_global_text()

    def rename_series(self, item):
        if item is None:
            return
        if item.column() != 0 and item.parent():
            item = item.parent().child(item.row(), 0)

        ss: SeriesState = item.data()
        if ss is None:
            return

        new_name, ok = QInputDialog.getText(
            self, "Rename Series", "Enter new name for series:",
            QLineEdit.Normal, ss.display_name
        )
        if ok and new_name.strip():
            ss.set_displayname(new_name.strip())

    def change_column_color(self, item):
        ss: SeriesState = item.data()
        if ss is None:
            return

        color_dialog = QColorDialog(ss.color, self)
        if color_dialog.exec():
            new_color = color_dialog.selectedColor()
            ss.set_color(new_color)

    def create_computed_series(self, item, mode):
        """整合計算序列 (微分與絕對值) 的處理邏輯"""
        ss: SeriesState = item.data()
        if ss.is_string_type:
            op_label = "absolute value" if mode == "abs" else "derivative"
            QMessageBox.warning(self, "Warning", f"Cannot calculate {op_label} of a string series.")
            return

        df = self.dataframes[ss.filepath]
        values = df[ss.column_name].to_numpy(dtype=float)

        if mode in (1, "1nd", "diff1"):
            timestamps = df.index.to_numpy(dtype=float) / 1e9
            dt = np.gradient(timestamps)
            dt[dt == 0] = 1e-9
            dy = np.gradient(values) / dt
            new_col_name = f"{ss.column_name}_1nd"
        elif mode in (2, "2nd", "diff2"):
            timestamps = df.index.to_numpy(dtype=float) / 1e9
            dt = np.gradient(timestamps)
            dt[dt == 0] = 1e-9
            dy1 = np.gradient(values) / dt
            dy = np.gradient(dy1) / dt
            new_col_name = f"{ss.column_name}_2nd"
        elif mode == "abs":
            dy = np.abs(values)
            new_col_name = f"{ss.column_name}_abs"
        else:
            return

        base_new_col_name = new_col_name
        idx = 0
        while new_col_name in df.columns:
            new_col_name = f"{base_new_col_name}.{idx}"
            idx += 1

        df[new_col_name] = dy

        file_item = item.parent()
        insert_row_idx = item.row() + 1
        new_key = (ss.filepath, new_col_name)

        if new_key not in self.series:
            self.series[new_key] = SeriesState(ss.filepath, new_col_name, ss.df, ss.timestamps, dy)

        file_item.insertRow(insert_row_idx, [self.series[new_key].column_item, self.series[new_key].value_item])
        self.series[new_key].column_item.setCheckState(Qt.Checked)

    def adjust_curve_position_scale(self, item):
        ss: SeriesState = item.data()
        current_y_offset, current_y_scale = ss.adjustment

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Adjust Curve: {ss.column_name}")
        layout = QFormLayout(dialog)

        y_offset_spin = QDoubleSpinBox(dialog)
        y_offset_spin.setRange(-100000000, 100000000)
        y_offset_spin.setValue(current_y_offset)
        y_offset_spin.setSingleStep(100.0)
        layout.addRow("Y Position Offset:", y_offset_spin)

        y_scale_spin = QDoubleSpinBox(dialog)
        y_scale_spin.setRange(0.00001, 100000)
        y_scale_spin.setValue(current_y_scale)
        y_scale_spin.setSingleStep(1.0)
        layout.addRow("Y Scale Factor:", y_scale_spin)

        button_box = QWidget()
        button_layout = QVBoxLayout(button_box)
        apply_button = QPushButton("Apply")
        reset_button = QPushButton("Reset")
        cancel_button = QPushButton("Cancel")
        button_layout.addWidget(apply_button)
        button_layout.addWidget(reset_button)
        button_layout.addWidget(cancel_button)
        layout.addRow("", button_box)

        apply_button.clicked.connect(lambda: ss.update_curve_adjustment(y_offset_spin.value(), y_scale_spin.value()))
        reset_button.clicked.connect(lambda: ss.update_curve_adjustment(0.0, 1.0))
        cancel_button.clicked.connect(dialog.reject)
        dialog.exec()

    def filter_tree_view(self, text):
        filter_text = text.lower()
        for row in range(self.model.rowCount()):
            file_item = self.model.item(row)
            if file_item:
                file_item_visible = False
                for child_row in range(file_item.rowCount()):
                    column_item = file_item.child(child_row)
                    if column_item:
                        column_name = column_item.text().lower()
                        if column_item.checkState() == Qt.Checked or filter_text in column_name:
                            self.tree_view.setRowHidden(child_row, file_item.index(), False)
                            file_item_visible = True
                        else:
                            self.tree_view.setRowHidden(child_row, file_item.index(), True)
                self.tree_view.setRowHidden(row, self.model.invisibleRootItem().index(), not file_item_visible)


    def update_viewbox_geometry(self):
        self.second_vb.setGeometry(self.main_vb.sceneBoundingRect())
        self.mouse_vb.setGeometry(self.main_vb.sceneBoundingRect())
        self.update_label_positions()

    def mouse_moved(self, pos):
        if self.keyboard_mode:
            return

        if self.mouse_vb.sceneBoundingRect().contains(pos):
            mouse_point = self.mouse_vb.mapSceneToView(pos)
            self.update_crosshair(mouse_point.x() * 1e9)

    def update_crosshair(self, timestamp):
        if timestamp is None:
            for ss in self.series.values():
                if ss.value_label:
                    ss.value_label.setVisible(False)
            return

        self.current_timestamp = int(timestamp)
        timestamp_us = timestamp / 1e9
        self.v_line.setPos(timestamp_us)
        self.v_line.setVisible(True)

        time_str = datetime.fromtimestamp(timestamp_us, tz=timezone(timedelta(seconds=-self.x_axis.utcOffset))).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        self.x_axis_label.setText(time_str)
        self.x_axis_label.setPos(timestamp_us, self.mouse_vb.viewRange()[1][0])

        # 更新樹狀視圖中的即時值
        for file_path, df in self.dataframes.items():
            # 效能優化：如果該檔案的樹狀節點未展開，且沒有任何啟用的曲線/文字被勾選，則略過更新以節省 CPU
            file_item = self.file_items.get(file_path)
            if file_item and not self.tree_view.isExpanded(file_item.index()):
                # 如果只是收合狀態，但裡面仍有打勾顯示的項目，可根據需求決定是否要跳過
                pass

            for column_name in df.columns:
                key = (file_path, column_name)
                ss = self.series.get(key)
                if not ss:
                    continue
                res = ss.search_by_timestamp(self.current_timestamp)
                if res is None:
                    continue
                value, _, _ = res
                value_item = ss.value_item
                if value_item:
                    if pd.isna(value):
                        value_item.setText("NaN")
                    elif isinstance(value, str):
                        value_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft)
                        value_item.setText(value)
                    else:
                        try:
                            value_item.setTextAlignment(Qt.AlignmentFlag.AlignRight)
                            value_item.setText(f"{value:.2f}" if isinstance(value, float) else str(value))
                        except (TypeError, ValueError):
                            value_item.setText(str(value))

        # 如果不在搜索模式，更新表格中的即時數據（保留置頂標記項目，重設即時數據顏色）
        if not self.in_search_mode:
            self._populate_table()

        # 顯示數值標籤
        for ss in self.series.values():
            if not ss.is_active or ss.value_label is None:
                continue

            label = ss.value_label

            if not self.show_values_mode:
                label.setVisible(False)
                continue

            res = ss.search_by_timestamp(self.current_timestamp)
            if res is None:
                label.setVisible(False)
                continue

            original_val, numeric_val, _ = res

            if pd.isna(original_val) or pd.isna(numeric_val):
                label.setVisible(False)
            else:
                y_offset, y_scale = ss.adjustment
                base = getattr(ss.curve, 'base_val', 0.0)
                adjusted_y = (numeric_val - base) * y_scale + base + y_offset

                if ss.is_string_type:
                    text = str(original_val)
                elif isinstance(original_val, float):
                    text = f"{original_val:.4f}"
                else:
                    text = str(original_val)
                label.setText(text)
                label.setVisible(True)
                label.setPos(timestamp_us, adjusted_y)

    def wheel_zoom(self, event):
        delta = event.angleDelta().y() / 120

        if event.modifiers() & Qt.ControlModifier:
            zoom_factor = 1.1 if delta > 0 else 0.9
            view_range = self.plot_widget.viewRange()
            current_x_min, current_x_max = view_range[0]
            current_x_range = current_x_max - current_x_min
            new_x_range = current_x_range * zoom_factor
            mouse_point = self.plot_widget.plotItem.vb.mapSceneToView(event.position())
            zoom_center = mouse_point.x()
            new_x_min = zoom_center - (zoom_center - current_x_min) * zoom_factor
            new_x_max = new_x_min + new_x_range
            self.plot_widget.setXRange(new_x_min, new_x_max, padding=0)
        else:
            scroll_speed = 0.1
            view_range = self.plot_widget.viewRange()
            current_x_min, current_x_max = view_range[0]
            current_x_range = current_x_max - current_x_min
            scroll_amount = current_x_range * scroll_speed * delta
            new_x_min = current_x_min - scroll_amount
            new_x_max = current_x_max - scroll_amount
            self.plot_widget.setXRange(new_x_min, new_x_max, padding=0)
        event.accept()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        self.load_csv_files(files)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F3:
            if event.modifiers() & Qt.ShiftModifier:
                self.find_previous_result()
            else:
                self.find_next_result()
            event.accept()
            return

        if event.key() == Qt.Key_F4:
            self.toggle_bottom_widget()
            event.accept()
            return

        if event.key() == Qt.Key_F1:
            self.show_usage_dialog()
            event.accept()
            return

        if event.key() == Qt.Key_F2:
            self.toggle_measure_mode()
            event.accept()
            return

        if event.key() == Qt.Key_Space:
            self.toggle_keyboard_mode()
            event.accept()
            return

        if event.key() == Qt.Key_M:
            self.toggle_show_markers()
            event.accept()
            return

        if event.key() == Qt.Key_S:
            self.toggle_show_values()
            event.accept()
            return

        if (self.keyboard_mode or self.in_search_mode) and self.dataframes and (event.key() == Qt.Key_Left or event.key() == Qt.Key_Right):
            first_file = next(iter(self.dataframes))
            df = self.dataframes[first_file]
            timestamps = df.index.to_numpy()

            if self.current_timestamp is None:
                current_idx = 0
            else:
                current_idx = np.searchsorted(timestamps, self.current_timestamp, side='right') - 1

            if event.key() == Qt.Key_Left:
                new_idx = max(0, current_idx - 1)
            elif event.key() == Qt.Key_Right:
                new_idx = min(len(timestamps) - 1, current_idx + 1)
            else:
                return

            if new_idx != current_idx:
                self.update_crosshair(timestamps[new_idx])
            event.accept()
            return

    def toggle_keyboard_mode(self, checked=None):
        if checked is None:
            self.keyboard_mode = not self.keyboard_mode
        else:
            self.keyboard_mode = bool(checked)
        if hasattr(self, 'keyboard_mode_action'):
            self.keyboard_mode_action.setChecked(self.keyboard_mode)

    def toggle_show_markers(self, checked=None):
        if checked is None:
            self.show_markers = not self.show_markers
        else:
            self.show_markers = bool(checked)
        if hasattr(self, 'show_markers_action'):
            self.show_markers_action.setChecked(self.show_markers)
        for ss in self.series.values():
            if not ss.is_active:
                continue
            ss.show_marker(self.show_markers)

    def toggle_show_values(self, checked=None):
        if checked is None:
            self.show_values_mode = not self.show_values_mode
        else:
            self.show_values_mode = bool(checked)
        if hasattr(self, 'show_values_action'):
            self.show_values_action.setChecked(self.show_values_mode)
        self.update_crosshair(self.current_timestamp)

    def toggle_bottom_widget(self, checked=None):
        if checked is None:
            is_visible = self.bottom_widget.isHidden()
        else:
            is_visible = bool(checked)
        self.bottom_widget.setVisible(is_visible)
        if hasattr(self, 'bottom_panel_action'):
            self.bottom_panel_action.setChecked(is_visible)
        if is_visible:
            sizes = self.main_splitter.sizes()
            if len(sizes) >= 2 and sizes[1] == 0:
                total = sum(sizes)
                self.main_splitter.setSizes([int(total * 0.85), int(total * 0.15)])

    def toggle_measure_mode(self, checked=None):
        if checked is None:
            self.measure_mode = not self.measure_mode
        else:
            self.measure_mode = bool(checked)
        if hasattr(self, 'measure_mode_action'):
            self.measure_mode_action.setChecked(self.measure_mode)

        self.measure_line1.setVisible(self.measure_mode)
        self.measure_line2.setVisible(self.measure_mode)
        self.measure_label.setVisible(self.measure_mode)
        self.measure_text1.setVisible(self.measure_mode)
        self.measure_text2.setVisible(self.measure_mode)

        if self.measure_mode:
            x_min, x_max = self.main_vb.viewRange()[0]

            pos1_val = self.measure_line1.value()
            pos2_val = self.measure_line2.value()
            if not (x_min < pos1_val < x_max and x_min < pos2_val < x_max):
                self.measure_line1.setValue(x_min + (x_max - x_min) * 0.3)
                self.measure_line2.setValue(x_min + (x_max - x_min) * 0.7)

            self.update_measure_label()

    def update_measure_label(self):
        if not self.measure_mode:
            return

        pos1 = self.measure_line1.value()
        pos2 = self.measure_line2.value()
        tz = timezone(timedelta(seconds=-self.x_axis.utcOffset))

        t1 = min(pos1, pos2)
        t2 = max(pos1, pos2)
        delta_t = t2 - t1
        summary_text = f"Δt: {delta_t:.6f} s"
        self.measure_label.setText(summary_text)

        pos1_str = datetime.fromtimestamp(pos1, tz=tz).strftime('%H:%M:%S.%f')[:-3]
        pos2_str = datetime.fromtimestamp(pos2, tz=tz).strftime('%H:%M:%S.%f')[:-3]
        self.measure_text1.setText(pos1_str)
        self.measure_text2.setText(pos2_str)

        y_min, y_max = self.main_vb.viewRange()[1]

        self.measure_label.setPos((pos1 + pos2)/2, y_min)
        self.measure_text1.setPos(pos1, y_min)
        self.measure_text2.setPos(pos2, y_min)

    # --- 全局搜索與表格功能方法 ---

    def _populate_table(self):
        """填充/刷新文字數據表格（支援搜索模式與即時數據模式，始終置頂顯示已標記項目並正確還原/設定顏色）。"""
        tz = timezone(timedelta(seconds=-self.x_axis.utcOffset))
        pinned_list = sorted(self.pinned_results.values(), key=lambda x: x['timestamp'])

        rows_data = []

        # 1. 置頂項目（帶有自訂標記顏色）
        for p in pinned_list:
            ts_str = datetime.fromtimestamp(p['timestamp'] / 1e9, tz=tz).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            filename = os.path.basename(p['file'])
            rows_data.append({
                'ts_str': ts_str,
                'file': filename,
                'filepath': p['file'],
                'series': p['series'],
                'value': p['value'],
                'pinned': True,
                'color': p['color'],
                'timestamp': p['timestamp']
            })

        if self.in_search_mode:
            # 2a. 搜索模式：加入搜索匹配項目（未置頂項目，使用預設顏色）
            pinned_keys = set(self.pinned_results.keys())
            for item in self.raw_search_matches:
                key = (item['file'], item['timestamp'], item['series'])
                if key not in pinned_keys:
                    ts_str = datetime.fromtimestamp(item['timestamp'] / 1e9, tz=tz).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                    filename = os.path.basename(item['file'])
                    rows_data.append({
                        'ts_str': ts_str,
                        'file': filename,
                        'filepath': item['file'],
                        'series': item['series'],
                        'value': item['value'],
                        'pinned': False,
                        'color': None,
                        'timestamp': item['timestamp']
                    })
        else:
            # 2b. 非搜索模式：加入當前時間點的實時文字數據（未置頂項目，還原預設顏色）
            if self.current_timestamp is not None:
                for key in self.active_text_series:
                    ss = self.series.get(key)
                    if not ss:
                        continue
                    res = ss.search_by_timestamp(self.current_timestamp)
                    if res is None:
                        continue
                    value, _, closest_timestamp = res
                    if isinstance(value, str) and value:
                        ts_str = datetime.fromtimestamp(closest_timestamp / 1e9, tz=tz).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                        rows_data.append({
                            'ts_str': ts_str,
                            'file': os.path.basename(ss.filepath),
                            'filepath': ss.filepath,
                            'series': ss.display_name,
                            'value': value,
                            'pinned': False,
                            'color': None,
                            'timestamp': closest_timestamp
                        })

        self.search_results = rows_data
        self.text_data_table.setRowCount(len(rows_data))

        default_brush = QBrush()

        for row_idx, r in enumerate(rows_data):
            col_texts = [r['ts_str'], r['file'], r['series'], r['value']]
            is_pinned = r['pinned']
            color = r['color']

            if is_pinned and color:
                bg_color = QColor(color.red(), color.green(), color.blue(), 70)
                bg_brush = QBrush(bg_color)
                fg_brush = QBrush(color.lighter(130) if color.value() < 180 else color)
            else:
                bg_brush = default_brush
                fg_brush = default_brush

            for col_idx, text in enumerate(col_texts):
                item = self.text_data_table.item(row_idx, col_idx)
                if item is None:
                    item = QTableWidgetItem(text)
                    self.text_data_table.setItem(row_idx, col_idx, item)
                else:
                    item.setText(text)

                item.setBackground(bg_brush)
                item.setForeground(fg_brush)

    def clear_all_pinned_results(self):
        """清除所有標記與置頂垂線。"""
        for pinned_info in self.pinned_results.values():
            if pinned_info.get('line'):
                try:
                    self.mouse_vb.removeItem(pinned_info['line'])
                except Exception:
                    pass
        self.pinned_results.clear()

        self._populate_table()
        if self.in_search_mode and self.search_results:
            self.jump_to_result(0, scroll_table=True)

    def show_table_context_menu(self, position):
        """顯示搜尋表格的右鍵選單。"""
        menu = QMenu(self)

        item = self.text_data_table.itemAt(position)
        if item is not None:
            row = item.row()
            if 0 <= row < len(self.search_results):
                result = self.search_results[row]
                key = (result['filepath'], result['timestamp'], result['series'])
                if key in self.pinned_results:
                    toggle_action = QAction("取消標記 (雙擊)", self)
                else:
                    toggle_action = QAction("標記 / 置頂垂線 (雙擊)", self)
                toggle_action.triggered.connect(lambda checked=False, it=item: self.jump_to_result_from_double_click(it))
                menu.addAction(toggle_action)
                menu.addSeparator()

        clear_action = QAction("清除所有標記", self)
        clear_action.triggered.connect(self.clear_all_pinned_results)
        clear_action.setEnabled(bool(self.pinned_results))
        menu.addAction(clear_action)

        menu.exec(self.text_data_table.viewport().mapToGlobal(position))

    def clear_search_if_empty(self, text):
        """如果搜索框被清空，則退出搜索模式（保留標記的置頂項目與垂線，還原即時文字數據及顏色）。"""
        if not text:
            self.in_search_mode = False
            self.raw_search_matches.clear()
            self.current_search_index = -1
            self.search_status_label.setText("")
            self._populate_table()
            self.update_crosshair(self.current_timestamp)

    def search_global_text(self):
        """在所有 active_text_series 中搜索文字（保留已標記置頂的結果）。"""
        search_text = self.search_input.text()
        if not search_text:
            self.clear_search_if_empty("")
            return

        self.in_search_mode = True
        self.raw_search_matches.clear()
        self.current_search_index = -1

        found_items = []
        for key in self.active_text_series:
            file_path, column_name = key
            df = self.dataframes[file_path]
            series = df[column_name]

            # 確保只在字串類型上操作
            string_series = series[series.apply(lambda x: isinstance(x, str))]
            if string_series.empty:
                continue

            # 進行不區分大小寫的包含匹配
            hits = string_series[string_series.str.contains(search_text, case=False, na=False)]

            ss = self.series.get(key)
            series_display = ss.display_name if ss else column_name
            for timestamp, value in hits.items():
                found_items.append({
                    'timestamp': timestamp,
                    'file': file_path,
                    'series': series_display,
                    'value': value
                })

        self.raw_search_matches = sorted(found_items, key=lambda x: x['timestamp'])
        self._populate_table()

        if self.search_results:
            self.jump_to_result(0)
        else:
            self.search_status_label.setText("未找到結果")

    def find_next_result(self):
        """跳轉到下一個搜索結果。"""
        if not self.search_results:
            return

        next_index = (self.current_search_index + 1) % len(self.search_results)
        self.jump_to_result(next_index)

    def find_previous_result(self):
        """跳轉到上一個搜索結果。"""
        if not self.search_results:
            return

        prev_index = (self.current_search_index - 1 + len(self.search_results)) % len(self.search_results)
        self.jump_to_result(prev_index)

    def jump_to_result_from_hover(self, row, column):
        """處理表格中的滑鼠懸停事件，跳轉到對應的搜索/置頂結果。"""
        if row == self.current_search_index:
            return
        if not (0 <= row < len(self.search_results)):
            return
        self.jump_to_result(row, scroll_table=False)

    def jump_to_result_from_double_click(self, item):
        """處理表格中的雙擊事件：切換置頂狀態、隨機顏色與圖表標記垂線。"""
        if item is None:
            return

        row = item.row()
        if not (0 <= row < len(self.search_results)):
            return

        result = self.search_results[row]
        key = (result['filepath'], result['timestamp'], result['series'])

        if key in self.pinned_results:
            pinned_info = self.pinned_results.pop(key)
            if pinned_info.get('line'):
                try:
                    self.mouse_vb.removeItem(pinned_info['line'])
                except Exception:
                    pass
        else:
            h = random.randint(0, 359)
            s = random.randint(160, 255)
            v = random.randint(200, 255)
            color = QColor.fromHsv(h, s, v)

            pen = pg.mkPen(color, width=1.5, style=Qt.DashLine)
            line = pg.InfiniteLine(pos=result['timestamp'] / 1e9, angle=90, movable=False, pen=pen)
            self.mouse_vb.addItem(line, ignoreBounds=True)

            self.pinned_results[key] = {
                'timestamp': result['timestamp'],
                'file': result['filepath'],
                'series': result['series'],
                'value': result['value'],
                'color': color,
                'line': line,
            }

        self._populate_table()

        target_index = 0
        for idx, res in enumerate(self.search_results):
            if (res['filepath'], res['timestamp'], res['series']) == key:
                target_index = idx
                break

        if self.search_results:
            self.jump_to_result(target_index, scroll_table=True)

    def jump_to_result(self, index, scroll_table=True):
        """將圖表和表格跳轉到指定的結果索引。"""
        if not (0 <= index < len(self.search_results)):
            return

        self.current_search_index = index
        result = self.search_results[index]

        # 更新狀態標籤（僅在搜尋模式下顯示幾分之幾）
        if self.in_search_mode:
            self.search_status_label.setText(f"結果: {index + 1}/{len(self.search_results)}")

        # 將圖表光標移動到結果的時間戳
        self.update_crosshair(result['timestamp'])

        # 滾動表格、選取該行並設置焦點
        if scroll_table:
            item = self.text_data_table.item(index, 0)
            if item:
                self.text_data_table.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
            self.text_data_table.setFocus()
        self.text_data_table.selectRow(index)





def main():
    # import pyqtgraph.examples
    # pyqtgraph.examples.run()
    # return

    app = QApplication(sys.argv)
    window = CSVPlotViewer()
    window.resize(1200, 800)
    window.show()

    parser = ArgumentParser(description=__doc__.strip())
    parser.add_argument('files', nargs='*', action="store", help='CSV file')
    args = parser.parse_args()
    if args.files:
        try:
            window.load_csv_files(args.files)
        except Exception as e:
            print(f"Error loading file {args.file}: {e}")
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
