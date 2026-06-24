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
from argparse import ArgumentParser
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QTreeView, QSplitter,
    QVBoxLayout, QColorDialog, QWidget, QMenuBar, QAbstractItemView, QMenu,
    QDialog, QFormLayout, QDoubleSpinBox, QPushButton, QLabel, QLineEdit, QMessageBox,
    QTableWidget, QTableWidgetItem, QHBoxLayout
)
from PySide6.QtGui import QAction, QStandardItemModel, QStandardItem, QColor, QBrush, QActionGroup
from PySide6.QtCore import Qt
import pyqtgraph as pg
from pyqtgraph import PlotWidget, DateAxisItem, PlotDataItem

VERSION = "20260603"


class DraggableLabelItem(pg.TextItem):
    def __init__(self, text, color, anchor, key, viewer):
        super().__init__(text=text, color=color, anchor=anchor)
        self.key = key
        self.viewer = viewer
        self.is_dragging = False
        self.drag_mode = None # 'offset' or 'scale'
        self.initial_mouse_y = 0
        self.initial_offset = 0
        self.initial_scale = 1.0
        self.update_color(color)

    def update_color(self, color):
        self.setColor(color)
        bg_color = QColor('black')
        # bg_color.setAlpha(40) # 加上透明背景，讓文字更明顯
        self.fill = pg.mkBrush(bg_color)
        self.border = pg.mkPen(color) # 加上同色邊框
        self.update()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            ev.accept()
            self.viewer.uncheck_series_by_key(self.key)
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
            # Get the current adjustments
            self.initial_offset, self.initial_scale = self.viewer.curve_adjustments.get(self.key, (0.0, 1.0))
        else:
            ev.ignore()

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
                self.viewer.update_curve_adjustment(self.key, new_offset, self.initial_scale)
            elif self.drag_mode == 'scale':
                view_range_y = vb.viewRange()[1]
                view_height = view_range_y[1] - view_range_y[0]
                if view_height == 0: return

                # Exponential scaling feels more natural
                scale_factor = np.exp(dy / (view_height / 2.0))
                new_scale = self.initial_scale * scale_factor
                new_scale = max(0.001, new_scale) # Clamp to a minimum value
                self.viewer.update_curve_adjustment(self.key, self.initial_offset, new_scale)
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
        self.curves = {}        # (file_path, column_name) -> (PlotDataItem, ViewBox)
        self.curve_labels = {}  # (file_path, column_name) -> TextItem
        self.curve_value_labels = {}  # (file_path, column_name) -> TextItem
        self.color_map = {}     # (file_path, column_name) -> QColor
        self.curve_adjustments = {}  # (file_path, column_name) -> (y_offset, y_scale)
        self.value_items = {}   # (file_path, column_name) -> QStandardItem (for live value display in tree)
        self.file_items = {}    # file_path -> QStandardItem (for tracking tree nodes)
        self.active_text_series = set() # 用於追蹤已勾選的字串系列

        # 初始化搜索功能相關變數
        self.search_results = []
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
        self.plot_widget.addLegend()
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

        self.second_vb = pg.ViewBox()
        self.plot_widget.showAxis('right')
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
        # self.mouse_vb.setMenuEnabled(False)


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

        self.search_prev_button.clicked.connect(self.find_previous_result)
        self.search_next_button.clicked.connect(self.find_next_result)

        self.search_layout.addWidget(QLabel("全局搜索:"))
        self.search_layout.addWidget(self.search_input)
        self.search_layout.addWidget(self.search_prev_button)
        self.search_layout.addWidget(self.search_next_button)
        self.search_layout.addWidget(self.search_status_label)
        self.search_layout.addStretch()

        self.bottom_layout.addWidget(self.search_widget)

        # 文字數據表格
        self.text_data_table = QTableWidget()
        self.text_data_table.setColumnCount(4)
        self.text_data_table.setHorizontalHeaderLabels(["Timestamp", "File", "Series", "Value"])
        self.text_data_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.text_data_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.text_data_table.horizontalHeader().setStretchLastSection(True)
        self.text_data_table.itemDoubleClicked.connect(self.jump_to_result_from_double_click)
        self.bottom_layout.addWidget(self.text_data_table)
        self.bottom_widget.setVisible(False) # 初始時隱藏整個下半部

        # 調整分割器比例
        self.main_splitter.setSizes([700, 300])

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

    def uncheck_series_by_key(self, key_to_uncheck):
        """Finds an item in the tree model by its key and unchecks it."""
        for row in range(self.model.rowCount()):
            file_item = self.model.item(row)
            if not file_item:
                continue

            # Check if the file path in the key matches the file item's path
            if file_item.data() == key_to_uncheck[0]:
                for child_row in range(file_item.rowCount()):
                    child_item = file_item.child(child_row)
                    if child_item and child_item.data() == key_to_uncheck:
                        child_item.setCheckState(Qt.Unchecked)
                        return # Found and unchecked, so we can exit

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
    <li><b>M:</b> 切換顯示曲線的資料點標記 (Marker)。</li>
    <li><b>S:</b> 切換顯示曲線數值，在滑鼠線上和各線段交點處顯示標註數值。</li>
</ul>
<h3>檔案與選單操作:</h3>
<ul>
    <li>將 CSV 檔案拖放到視窗中以載入。</li>
    <li><b>File -> Open CSV Files:</b> 選擇並載入 CSV 檔案。</li>
    <li><b>Timezone:</b> 切換時區，以不同 UTC 偏移量顯示時間。</li>
</ul>
<h3>樹狀圖與清單操作:</h3>
<ul>
    <li><b>過濾框 (Filter):</b> 輸入文字過濾顯示的資料列。</li>
    <li><b>右鍵選單 (檔案層級):</b> 載入該檔案的訊號描述檔 (SignalList)、清除該檔案所有選取、移除該檔案。</li>
    <li><b>右鍵選單 (資料列層級):</b> 更改曲線顏色、手動調整 Y 軸偏移與縮放、產生一次微分 (_1nd)、二次微分 (_2nd) 及絕對值 (_abs) 資料。</li>
    <li><b>右鍵選單 (通用):</b> 展開/折疊全部、勾選/取消勾選所有過濾結果、移除所有檔案。</li>
</ul>
<h3>搜尋與文字資料功能:</h3>
<ul>
    <li>在右側樹状圖中勾選要搜尋的文字序列。</li>
    <li>在下方的搜尋框中輸入文字並按 Enter 鍵。</li>
    <li>搜尋結果會顯示在下方表格。雙擊結果可跳轉至對應時間點。</li>
</ul>
        """
        QMessageBox.about(self, "LogPlot Usage", help_text)

    def set_timezone_offset(self, offset):
        self.x_axis.utcOffset = -offset * 3600
        self.x_axis.picture = None
        self.x_axis.update()
        self.plot_widget.update()


    def select_csv_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Open CSV Files", "", "CSV Files (*.csv *.csv.zst *.csv.zstd)"
        )
        if file_paths:
            self.load_csv_files(file_paths)

    def select_signallist_for_file(self, file_item):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Load SignalList Files", "", "CSV Files (*.csv)"
        )
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
                    key = child_item.data()
                    col_name = key[1]
                    color = self.color_map.get(key, QColor(Qt.white)).name()
                    offset, scale = self.curve_adjustments.get(key, (0.0, 1.0))
                    state["series"].append({
                        "file_path": fpath,
                        "column_name": col_name,
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

        for s in state.get("series", []):
            key = (s["file_path"], s["column_name"])
            if key in self.color_map:
                self.color_map[key] = QColor(s.get("color", "#FFFFFF"))

            self.curve_adjustments[key] = (s.get("y_offset", 0.0), s.get("y_scale", 1.0))

            for row in range(self.model.rowCount()):
                file_item = self.model.item(row)
                if file_item and file_item.data() == s["file_path"]:
                    for child_row in range(file_item.rowCount()):
                        child_item = file_item.child(child_row)
                        if child_item and child_item.data() == key:
                            child_item.setForeground(QBrush(self.color_map.get(key, QColor(Qt.white))))
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

            if (nn := child_item.text()) in signallist:
                child_item.setText(nn + ": " + signallist[nn])


    def _populate_tree(self, file_path, df):
        filename = os.path.basename(file_path)
        file_item = QStandardItem(filename)
        file_item.setCheckable(False)
        file_item.setData(file_path) # 儲存完整路徑以便後續操作
        self.file_items[file_path] = file_item

        for col in df.columns:
            key = (file_path, col)

            if key not in self.color_map:
                h = random.randint(0, 359)
                s = random.randint(150, 255)
                v = random.randint(200, 255)
                self.color_map[key] = QColor.fromHsv(h, s, v)

            color = self.color_map[key]

            child_item = QStandardItem(col)
            child_item.setCheckable(True)
            child_item.setData(key)
            child_item.setForeground(QBrush(color))

            value_item = QStandardItem("")
            value_item.setEditable(False)
            self.value_items[key] = value_item
            file_item.appendRow([child_item, value_item])

        # Add to model after all children are appended to avoid multiple UI refresh signals
        self.model.appendRow(file_item)


    def _create_or_update_curve(self, curve, key):
        file_path, column_name = key
        df = self.dataframes[file_path]
        timestamps = df.index.astype(np.int64) / 1e9

        is_string_type = (df[column_name].dtype in ['object', 'str'])
        if is_string_type and not column_name.endswith('_numeric'):
            unique_vals = df[column_name].dropna().unique()
            if 1 < len(unique_vals) < 10:
                mapping = {val: i for i, val in enumerate(unique_vals)}
                values = df[column_name].map(mapping).to_numpy(dtype=float)
            else: # Should not happen for a plotted curve, but as a safeguard
                values = np.full(len(timestamps), np.nan)
        else:
            values = df[column_name].to_numpy(dtype=float)

        # print(values)
        color_key = (file_path, column_name)
        if color_key not in self.color_map and column_name.endswith('_numeric'):
            original_col_name = column_name.removesuffix('_numeric')
            color_key = (file_path, original_col_name)
        color = self.color_map[color_key]

        adj_key = key
        if adj_key not in self.curve_adjustments and column_name.endswith('_numeric'):
            original_col_name = column_name.removesuffix('_numeric')
            adj_key = (file_path, original_col_name)
        y_offset, y_scale = self.curve_adjustments.get(adj_key, (0.0, 1.0))

        if np.isnan(values).all():
            base = 0.0
            min_val = np.nan
            max_val = np.nan
        else:
            base = np.nanmin(values)
            min_val = base
            max_val = np.nanmax(values)

        adjusted_values = (values - base) * y_scale + base + y_offset

        filename = os.path.basename(file_path)
        legend_name = f"{column_name} ({filename}) (offset={y_offset:.2f}, scale={y_scale:.2f})"
        if curve is None:
            curve = PlotDataItem(x=timestamps, y=adjusted_values, pen=pg.mkPen(color=color), paint=None, name=legend_name)
        else:
            curve.setData(x=timestamps, y=adjusted_values, name=legend_name)

        if self.show_markers:
            curve.setSymbol('x')
            curve.setSymbolSize(4)
            curve.setSymbolBrush(color)
            curve.setSymbolPen(color)
        else:
            curve.setSymbol(None)

        # 紀錄 Y 軸的最大與最小值，供 Label 定位在垂直置中時使用
        curve.y_min_val = (min_val - base) * y_scale + base + y_offset
        curve.y_max_val = (max_val - base) * y_scale + base + y_offset
        curve.base_val = base

        return curve, min_val, max_val

    def on_item_changed(self, item):
        if not item.isCheckable() or not item.data():
            return

        key = tuple(item.data())
        file_path, column_name = key
        df = self.dataframes[file_path]
        is_string_type = (df[column_name].dtype in ['object', 'str'])

        if item.checkState() == Qt.Checked:
            if is_string_type:
                if key in self.curves:
                    return

                # 檢查獨立字串數量，如果小於10，則當作分類數據繪圖
                unique_vals = df[column_name].dropna().unique()
                if 1 < len(unique_vals) < 10:
                    # 建立字串到整數的映射
                    mapping = {val: i for i, val in enumerate(unique_vals)}
                    df[column_name + '_numeric'] = df[column_name].map(mapping)

                    # 為新的分類曲線計算一個不重疊的 Y 軸偏移
                    if key not in self.curve_adjustments:
                        y_max_values = [c.y_max_val for c, v in self.curves.values() if v == self.second_vb and hasattr(c, 'y_max_val') and not np.isnan(c.y_max_val)]

                        new_offset = max(y_max_values) + 1.5 if y_max_values else 0.0
                        self.curve_adjustments[key] = (new_offset, 1.0)

                    # 建立曲線
                    curve, _, _ = self._create_or_update_curve(None, (file_path, column_name + '_numeric'))
                    vb = self.second_vb # 在右邊的 ViewBox 繪製
                    vb.addItem(curve)
                    self.curves[key] = (curve, vb)

                    # 為字串轉換的曲線也加上標籤
                    _, col_name_only = key
                    color = self.color_map[key]
                    label = DraggableLabelItem(text=col_name_only, color=color, anchor=(-0.1, 0.5), key=key, viewer=self)
                    self.curve_labels[key] = label
                    vb.addItem(label, ignoreBounds=True)

                    value_label = pg.TextItem("", color=color, anchor=(-0.1, 0.5))
                    value_label.setVisible(False)
                    bg_color = QColor('black')
                    bg_color.setAlpha(150)
                    value_label.fill = pg.mkBrush(bg_color)
                    vb.addItem(value_label, ignoreBounds=True)
                    self.curve_value_labels[key] = value_label

                    if vb.state['autoRange'][1]:
                        y_bounds = vb.childrenBounds()[1]
                        if y_bounds:
                            vb.setYRange(y_bounds[0], y_bounds[1], padding=0.05)
                        else:
                            vb.setYRange(0, 1)
                        vb.state['autoRange'][1] = True
                    self.update_label_positions()
                    df.drop(columns=[column_name + '_numeric'], inplace=True)
                else:
                    self.active_text_series.add(key)
                    self.update_crosshair(self.current_timestamp) # Refresh table for live mode
            else:
                if key in self.curves:
                    return

                values = df[column_name].to_numpy(dtype=float)
                if np.isnan(values).all():
                    min_val = np.nan
                    max_val = np.nan
                else:
                    min_val = np.nanmin(values)
                    max_val = np.nanmax(values)

                if min_val < 10 and max_val < 10:
                    if key not in self.curve_adjustments:
                        y_max_values = [c.y_max_val for c, v in self.curves.values() if v == self.second_vb and hasattr(c, 'y_max_val') and not np.isnan(c.y_max_val)]
                        new_offset = max(y_max_values) + 1.5 if y_max_values else 0.0
                        self.curve_adjustments[key] = (new_offset, 1.0)

                curve, _, _ = self._create_or_update_curve(None, key)
                vb = self.second_vb if min_val < 10 and max_val < 10 else self.main_vb
                vb.addItem(curve)
                self.curves[key] = (curve, vb)

                _, col_name_only = key
                color = self.color_map[key]
                label = DraggableLabelItem(text=col_name_only, color=color, anchor=(-0.1, 0.5), key=key, viewer=self)
                self.curve_labels[key] = label
                vb.addItem(label, ignoreBounds=True)

                value_label = pg.TextItem("", color=color, anchor=(-0.1, 0.5))
                value_label.setVisible(False)
                bg_color = QColor('black')
                bg_color.setAlpha(150)
                value_label.fill = pg.mkBrush(bg_color)
                vb.addItem(value_label, ignoreBounds=True)
                self.curve_value_labels[key] = value_label

                if vb.state['autoRange'][1]:
                    y_bounds = vb.childrenBounds()[1]
                    if y_bounds:
                        vb.setYRange(y_bounds[0], y_bounds[1], padding=0.05)
                    else:
                        vb.setYRange(0, 1)
                    vb.state['autoRange'][1] = True
                self.update_label_positions()

        elif item.checkState() == Qt.Unchecked:
            if is_string_type:
                if key in self.curves:
                    curve, vb = self.curves.pop(key)
                    vb.removeItem(curve)
                    if key in self.curve_labels:
                        label = self.curve_labels.pop(key)
                        vb.removeItem(label)
                    if key in self.curve_value_labels:
                        val_label = self.curve_value_labels.pop(key)
                        vb.removeItem(val_label)
                    if vb.state['autoRange'][1]:
                        y_bounds = vb.childrenBounds()[1]
                        if y_bounds:
                            vb.setYRange(y_bounds[0], y_bounds[1], padding=0.05)
                        else:
                            vb.setYRange(0, 1)
                        vb.state['autoRange'][1] = True
                elif key in self.active_text_series:
                    self.active_text_series.remove(key)
                    # If in search mode, re-run the search. Otherwise, update live view.
                    if self.in_search_mode:
                        self.search_global_text()
                    else:
                        self.update_crosshair(self.current_timestamp) # Refresh table for live mode
            else:
                if key in self.curves:
                    curve, vb = self.curves.pop(key)
                    vb.removeItem(curve)
                    if key in self.curve_labels:
                        label = self.curve_labels.pop(key)
                        vb.removeItem(label)
                    if key in self.curve_value_labels:
                        val_label = self.curve_value_labels.pop(key)
                        vb.removeItem(val_label)
                    if vb.state['autoRange'][1]:
                        y_bounds = vb.childrenBounds()[1]
                        if y_bounds:
                            vb.setYRange(y_bounds[0], y_bounds[1], padding=0.05)
                        else:
                            vb.setYRange(0, 1)
                        vb.state['autoRange'][1] = True

    def update_label_positions(self):
        x_min, _ = self.main_vb.viewRange()[0]

        for key, (curve, vb) in self.curves.items():
            if key not in self.curve_labels:
                continue

            label = self.curve_labels[key]

            if not hasattr(curve, 'y_min_val') or np.isnan(curve.y_min_val):
                label.setVisible(False)
                continue

            # 將標籤維持在 Y 軸可視最大最小值的中間
            y_pos = (curve.y_min_val + curve.y_max_val) / 2.0

            label.setVisible(True)
            label.setPos(x_min, y_pos)

    def show_context_menu(self, position):
        index = self.tree_view.indexAt(position)
        menu = QMenu()

        if index.isValid():
            item = self.model.itemFromIndex(index)
            if item.parent() is None:
                clear_file_selections_action = QAction("Clear All Selections", self)
                clear_file_selections_action.triggered.connect(
                    lambda checked=False, file_item=item: self.clear_all_selections_for_file(file_item)
                )
                menu.addAction(clear_file_selections_action)

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
            else:
                change_color_action = QAction("Change Color", self)
                change_color_action.triggered.connect(lambda: self.change_column_color(item))
                menu.addAction(change_color_action)
                adjust_curve_action = QAction("Adjust Y Position and Scale", self)
                adjust_curve_action.triggered.connect(lambda: self.adjust_curve_position_scale(item))
                menu.addAction(adjust_curve_action)

                menu.addSeparator()

                diff1_action = QAction("1st Derivative", self)
                diff1_action.triggered.connect(lambda: self.create_derivative_series(item, 1))
                menu.addAction(diff1_action)

                diff2_action = QAction("2nd Derivative", self)
                diff2_action.triggered.connect(lambda: self.create_derivative_series(item, 2))
                menu.addAction(diff2_action)

                abs_action = QAction("Absolute Value", self)
                abs_action.triggered.connect(lambda: self.create_absolute_series(item))
                menu.addAction(abs_action)

            menu.addSeparator()

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

        remove_all_action = QAction("Remove All Files", self)
        remove_all_action.triggered.connect(self.remove_all_files)
        menu.addAction(remove_all_action)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

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

        # 1. 移除所有相關的曲線和標籤
        keys_to_remove = [key for key in self.curves if key[0] == file_path]
        for key in keys_to_remove:
            curve, vb = self.curves.pop(key)
            vb.removeItem(curve)
            if key in self.curve_labels:
                label = self.curve_labels.pop(key)
                vb.removeItem(label)
            if key in self.curve_value_labels:
                val_label = self.curve_value_labels.pop(key)
                vb.removeItem(val_label)

        # 2. 清理所有相關的字典
        dict_keys_to_remove = [key for key in self.color_map if key[0] == file_path]
        for key in dict_keys_to_remove:
            self.color_map.pop(key, None)
            self.curve_adjustments.pop(key, None)
            self.value_items.pop(key, None)
        self.file_items.pop(file_path, None)

        # 3. 清理已勾選的文字序列
        series_to_remove = {s for s in self.active_text_series if s[0] == file_path}
        self.active_text_series -= series_to_remove

        # 4. 移除 DataFrame
        self.dataframes.pop(file_path, None)

        # 5. 從 TreeView 模型中移除項目
        self.model.removeRow(file_item.row())

        # 6. 如果在搜索模式下，刷新搜索結果
        if self.in_search_mode:
            self.search_global_text()

    def change_column_color(self, item):
        key = item.data()

        current_color = self.color_map[key]
        color_dialog = QColorDialog(current_color, self)
        if color_dialog.exec():
            new_color = color_dialog.selectedColor()
            self.color_map[key] = new_color

            item.setForeground(QBrush(new_color))

            if key in self.curves:
                curve, _ = self.curves[key]
                curve.setPen(color=new_color)
                if key in self.curve_labels:
                    self.curve_labels[key].update_color(new_color)

    def create_derivative_series(self, item, order):
        key = item.data()
        file_path, column_name = key
        df = self.dataframes[file_path]

        is_string_type = df[column_name].dtype == 'object'
        if is_string_type:
            QMessageBox.warning(self, "Warning", "Cannot calculate derivative of a string series.")
            return

        values = df[column_name].to_numpy(dtype=float)
        timestamps = df.index.to_numpy(dtype=float) / 1e9

        dt = np.gradient(timestamps)
        # Avoid division by zero
        dt[dt == 0] = 1e-9

        if order == 1:
            dy = np.gradient(values) / dt
            new_col_name = f"{column_name}_1nd"
        elif order == 2:
            dy1 = np.gradient(values) / dt
            dy = np.gradient(dy1) / dt
            new_col_name = f"{column_name}_2nd"

        base_new_col_name = new_col_name
        idx = 0
        while new_col_name in df.columns:
            new_col_name = f"{base_new_col_name}.{idx}"
            idx += 1

        df[new_col_name] = dy

        file_item = item.parent()
        insert_row_idx = item.row() + 1
        new_key = (file_path, new_col_name)

        if new_key not in self.color_map:
            h = random.randint(0, 359)
            s = random.randint(150, 255)
            v = random.randint(200, 255)
            self.color_map[new_key] = QColor.fromHsv(h, s, v)

        color = self.color_map[new_key]

        child_item = QStandardItem(new_col_name)
        child_item.setCheckable(True)
        child_item.setData(new_key)
        child_item.setForeground(QBrush(color))

        value_item = QStandardItem("")
        value_item.setEditable(False)
        self.value_items[new_key] = value_item
        file_item.insertRow(insert_row_idx, [child_item, value_item])

        child_item.setCheckState(Qt.Checked)

    def create_absolute_series(self, item):
        key = item.data()
        file_path, column_name = key
        df = self.dataframes[file_path]

        is_string_type = df[column_name].dtype == 'object'
        if is_string_type:
            QMessageBox.warning(self, "Warning", "Cannot calculate absolute value of a string series.")
            return

        values = df[column_name].to_numpy(dtype=float)
        dy = np.abs(values)
        new_col_name = f"{column_name}_abs"

        base_new_col_name = new_col_name
        idx = 0
        while new_col_name in df.columns:
            new_col_name = f"{base_new_col_name}.{idx}"
            idx += 1

        df[new_col_name] = dy

        file_item = item.parent()
        insert_row_idx = item.row() + 1
        new_key = (file_path, new_col_name)

        if new_key not in self.color_map:
            h = random.randint(0, 359)
            s = random.randint(150, 255)
            v = random.randint(200, 255)
            self.color_map[new_key] = QColor.fromHsv(h, s, v)

        color = self.color_map[new_key]

        child_item = QStandardItem(new_col_name)
        child_item.setCheckable(True)
        child_item.setData(new_key)
        child_item.setForeground(QBrush(color))

        value_item = QStandardItem("")
        value_item.setEditable(False)
        self.value_items[new_key] = value_item
        file_item.insertRow(insert_row_idx, [child_item, value_item])

        child_item.setCheckState(Qt.Checked)

    def adjust_curve_position_scale(self, item):
        key = item.data()
        current_y_offset, current_y_scale = self.curve_adjustments.get(key, (0.0, 1.0))
        _, column_name = key
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Adjust Curve: {column_name}")
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

        apply_button.clicked.connect(lambda: self._apply_curve_adjustments(key, y_offset_spin.value(), y_scale_spin.value(), dialog))
        reset_button.clicked.connect(lambda: self._apply_curve_adjustments(key, 0.0, 1.0, dialog))
        cancel_button.clicked.connect(dialog.reject)
        dialog.exec()

    def _apply_curve_adjustments(self, key, y_offset, y_scale, dialog):
        if key not in self.curves:
            return

        self.curve_adjustments[key] = (y_offset, y_scale)

        curve, vb = self.curves[key]
        vb.removeItem(curve)
        curve, min_val, max_val = self._create_or_update_curve(curve, key)
        vb.addItem(curve)

        self.update_label_positions()

    def update_curve_adjustment(self, key, y_offset, y_scale):
        if key not in self.curves:
            return

        self.curve_adjustments[key] = (y_offset, y_scale)
        curve, _ = self.curves[key]
        self._create_or_update_curve(curve, key)
        self.update_label_positions()

    def clear_all_selections_for_file(self, file_item):
        for row in range(file_item.rowCount()):
            column_item = file_item.child(row)
            if column_item and column_item.isCheckable() and column_item.checkState() == Qt.Checked:
                column_item.setCheckState(Qt.Unchecked)

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
        if self.keyboard_mode or self.in_search_mode:
            return

        if self.mouse_vb.sceneBoundingRect().contains(pos):
            mouse_point = self.mouse_vb.mapSceneToView(pos)
            self.update_crosshair(mouse_point.x() * 1e9)

    def update_crosshair(self, timestamp):
        if timestamp is None:
            if not self.in_search_mode:
                self.bottom_widget.setVisible(False)
            for label in self.curve_value_labels.values():
                label.setVisible(False)
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

            idx = df.index.searchsorted(self.current_timestamp, side='right') - 1
            if idx < 0:
                continue
            closest_row = df.iloc[idx]
            for column_name in df.columns:
                key = (file_path, column_name)
                value = closest_row[column_name]
                value_item = self.value_items.get(key)
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

        # 如果不在搜索模式，則更新表格為實時數據
        if not self.in_search_mode:
            tz = timezone(timedelta(seconds=-self.x_axis.utcOffset))

            display_data = []
            for file_path, df in self.dataframes.items():
                idx = df.index.searchsorted(self.current_timestamp, side='right') - 1
                if idx < 0: continue
                closest_row = df.iloc[idx]
                closest_timestamp = df.index[idx]

                for column_name in df.columns:
                    key = (file_path, column_name)
                    if key in self.active_text_series:
                        value = closest_row[column_name]
                        if isinstance(value, str) and value:
                            ts_str = datetime.fromtimestamp(closest_timestamp / 1e9, tz=tz).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                            display_data.append((ts_str, os.path.basename(file_path), column_name, value))

            self.text_data_table.setRowCount(len(display_data))

            for row_idx, row_data in enumerate(display_data):
                for col_idx, text in enumerate(row_data):
                    item = self.text_data_table.item(row_idx, col_idx)
                    if item is None:
                        self.text_data_table.setItem(row_idx, col_idx, QTableWidgetItem(text))
                    else:
                        item.setText(text)

            if display_data:
                self.text_data_table.resizeColumnsToContents()
                self.text_data_table.horizontalHeader().setStretchLastSection(True)

            self.bottom_widget.setVisible(bool(display_data))

        # 顯示數值標籤
        for key, (curve, vb) in self.curves.items():
            label = self.curve_value_labels.get(key)
            if not label:
                continue

            if not self.show_values_mode:
                label.setVisible(False)
                continue

            file_path, column_name = key
            df = self.dataframes[file_path]
            idx = df.index.searchsorted(self.current_timestamp, side='right') - 1
            if idx < 0 or idx >= len(df):
                label.setVisible(False)
                continue

            original_val = df.iloc[idx][column_name]
            is_string_type = (df[column_name].dtype in ['object', 'str'])

            if is_string_type and isinstance(original_val, str):
                unique_vals = df[column_name].dropna().unique()
                mapping = {val: i for i, val in enumerate(unique_vals)}
                val = mapping.get(original_val, np.nan)
            else:
                val = original_val

            if pd.isna(val):
                label.setVisible(False)
            else:
                y_offset, y_scale = self.curve_adjustments.get(key, (0.0, 1.0))
                base = getattr(curve, 'base_val', 0.0)
                adjusted_y = (val - base) * y_scale + base + y_offset

                if is_string_type:
                    text = str(original_val)
                elif isinstance(val, float):
                    text = f"{val:.4f}"
                else:
                    text = str(val)
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

        if event.key() == Qt.Key_F1:
            self.show_usage_dialog()
            event.accept()
            return

        if event.key() == Qt.Key_F2:
            self.toggle_measure_mode()
            event.accept()
            return

        if event.key() == Qt.Key_Space:
            self.keyboard_mode = not self.keyboard_mode
            event.accept()
            return

        if event.key() == Qt.Key_M:
            self.show_markers = not self.show_markers
            for key, (curve, _) in self.curves.items():
                if self.show_markers:
                    color = self.color_map[key]
                    curve.setSymbol('x')
                    curve.setSymbolSize(4)
                    curve.setSymbolBrush(color)
                    curve.setSymbolPen(color)
                else:
                    curve.setSymbol(None)
            event.accept()
            return

        if event.key() == Qt.Key_S:
            self.show_values_mode = not self.show_values_mode
            self.update_crosshair(self.current_timestamp)
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

    def toggle_measure_mode(self):
        self.measure_mode = not self.measure_mode

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

    # --- 全局搜索功能方法 ---

    def clear_search_if_empty(self, text):
        """如果搜索框被清空，則退出搜索模式。"""
        if not text:
            self.in_search_mode = False
            self.search_results.clear()
            self.current_search_index = -1
            self.search_status_label.setText("")
            # 刷新表格以顯示實時數據
            self.update_crosshair(self.current_timestamp)

    def search_global_text(self):
        """在所有 active_text_series 中搜索文字。"""
        search_text = self.search_input.text()
        if not search_text:
            self.clear_search_if_empty("")
            return

        self.in_search_mode = True
        self.search_results.clear()
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

            for timestamp, value in hits.items():
                found_items.append({
                    'timestamp': timestamp,
                    'file': file_path,
                    'series': column_name,
                    'value': value
                })

        # 按時間戳排序結果
        self.search_results = sorted(found_items, key=lambda x: x['timestamp'])
        self._populate_table_from_search_results()

        if self.search_results:
            self.jump_to_result(0)
        else:
            self.search_status_label.setText("未找到結果")

    def _populate_table_from_search_results(self):
        """用搜索結果填充表格。"""
        self.text_data_table.setRowCount(0)
        if not self.search_results:
            # self.bottom_widget.setVisible(False)
            return

        self.bottom_widget.setVisible(True)
        self.text_data_table.setRowCount(len(self.search_results))
        tz = timezone(timedelta(seconds=-self.x_axis.utcOffset))

        for row, result in enumerate(self.search_results):
            ts_str = datetime.fromtimestamp(result['timestamp'] / 1e9, tz=tz).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            filename = os.path.basename(result['file'])

            self.text_data_table.setItem(row, 0, QTableWidgetItem(ts_str))
            self.text_data_table.setItem(row, 1, QTableWidgetItem(filename))
            self.text_data_table.setItem(row, 2, QTableWidgetItem(result['series']))
            self.text_data_table.setItem(row, 3, QTableWidgetItem(result['value']))

        self.text_data_table.resizeColumnsToContents()
        self.text_data_table.horizontalHeader().setStretchLastSection(True)


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

    def jump_to_result_from_double_click(self, item):
        """處理表格中的雙擊事件，跳轉到對應的搜索結果。"""
        if not self.in_search_mode or item is None:
            return
        self.jump_to_result(item.row())

    def jump_to_result(self, index):
        """將圖表和表格跳轉到指定的結果索引。"""
        if not (0 <= index < len(self.search_results)):
            return

        self.current_search_index = index
        result = self.search_results[index]

        # 更新狀態標籤
        self.search_status_label.setText(f"結果: {index + 1}/{len(self.search_results)}")

        # 將圖表光標移動到結果的時間戳
        self.update_crosshair(result['timestamp'])

        # 滾動表格、選取該行並設置焦點
        self.text_data_table.scrollToItem(self.text_data_table.item(index, 0), QAbstractItemView.ScrollHint.PositionAtCenter)
        self.text_data_table.selectRow(index)
        self.text_data_table.setFocus()





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
