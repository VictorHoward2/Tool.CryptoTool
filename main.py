import sys
import os
import json
import asyncio
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QLabel, QTableWidget, QTableWidgetItem, QSplitter,
    QHBoxLayout, QGroupBox, QCheckBox, QPushButton, QFrame
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import (
    Qt, QThread, Signal, Slot, QUrl, QStandardPaths,
    QPropertyAnimation, QEasingCurve
)
from PySide6.QtGui import QColor, QAction

# NOTE: websockets is an asyncio library
import websockets

# ------------------------
# Helper / REST fetch
# ------------------------
def calc_signal(row):
    if pd.isna(row.get("RSI")) or pd.isna(row.get("EMA9")) or pd.isna(row.get("EMA21")):
        return ""
    if row["RSI"] > 70:
        return "Short"
    elif row["RSI"] < 30:
        return "Long"
    elif row["EMA9"] > row["EMA21"]:
        return "Long"
    elif row["EMA9"] < row["EMA21"]:
        return "Short"
    else:
        return "Neutral"

def fetch_ohlcv(symbol="SOLUSDT", interval="1m", limit=50):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    data = r.json()

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_base", "taker_quote", "ignore"
    ])
    df["open_time"] = df["open_time"].astype(int)
    df["close_time"] = df["close_time"].astype(int)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)

    # Time columns and indicators
    df["open_dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert("Asia/Ho_Chi_Minh")
    df["close_dt"] = pd.to_datetime(df["close_time"], unit="ms", utc=True).dt.tz_convert("Asia/Ho_Chi_Minh")
    df["Timeframe"] = df["open_dt"].dt.strftime("%H:%M:%S") + " - " + df["close_dt"].dt.strftime("%H:%M:%S")

    df["RSI"] = ta.rsi(df["close"], length=14)
    df["EMA9"] = ta.ema(df["close"], length=9)
    df["EMA21"] = ta.ema(df["close"], length=21)

    # Signal placeholder (calc later)
    df["Signal"] = df.apply(calc_signal, axis=1)

    return df

# ------------------------
# WebSocket worker (runs in QThread)
# ------------------------
class KlineWorker(QThread):
    # Emit a dict with kline data when received
    kline_received = Signal(dict)
    error = Signal(str)

    def __init__(self, symbol="solusdt", interval="1m"):
        super().__init__()
        self.symbol = symbol.lower()
        self.interval = interval
        self._running = True
        self.uri = f"wss://fstream.binance.com/ws/{self.symbol}@kline_{self.interval}"

    def stop(self):
        self._running = False

    def run(self):
        # Create and run an asyncio event loop in this thread
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._ws_loop())
        except Exception as e:
            self.error.emit(str(e))

    async def _ws_loop(self):
        # Reconnect loop
        while self._running:
            try:
                async with websockets.connect(self.uri, ping_interval=20, ping_timeout=10) as ws:
                    while self._running:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        # Binance kline frame located at data['k']
                        k = data.get("k", {})
                        if not k:
                            continue

                        # Build a minimal kline payload
                        payload = {
                            "open_time": int(k.get("t")),
                            "close_time": int(k.get("T")),
                            "open": float(k.get("o")),
                            "high": float(k.get("h")),
                            "low": float(k.get("l")),
                            "close": float(k.get("c")),
                            "volume": float(k.get("v")),
                            "is_closed": bool(k.get("x")),  # whether candle closed
                        }
                        # Emit to main thread
                        self.kline_received.emit(payload)
            except Exception as e:
                # emit error and wait a short time before reconnect
                self.error.emit(f"WS error: {e}")
                await asyncio.sleep(2)

# ------------------------
# MainWindow
# ------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SOLUSDT Futures Monitor (Realtime)")
        self.resize(1600, 800)

        # initial data
        self.df = fetch_ohlcv(limit=100).sort_values("open_time", ascending=True).reset_index(drop=True)

        # columns (used both for header labels and panel)
        self.columns = [
            "Timeframe", "Open", "High", "Low", "Close", "Volume",
            "RSI", "EMA9", "EMA21", "Signal", "Closed?"
        ]

        # layout
        splitter = QSplitter(Qt.Horizontal)

        # left: web view
        self.webview = QWebEngineView()
        try:
            self.webview.setUrl(QUrl("https://www.binance.com/en/futures/SOLUSDT"))
        except Exception:
            self.webview.setUrl("https://www.binance.com/en/futures/SOLUSDT")
        splitter.addWidget(self.webview)

        # right: indicators + panel
        right_widget = QWidget()
        right_layout = QVBoxLayout()

        # Price label polished
        self.price_label = QLabel("Current Price: ...")
        self.price_label.setAlignment(Qt.AlignCenter)
        font = self.price_label.font()
        font.setPointSize(16)
        font.setBold(True)
        self.price_label.setFont(font)

        # Control bar (price)
        controls = QWidget()
        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self.price_label)
        controls.setLayout(controls_layout)

        # Status bar
        self.status = self.statusBar()
        self.status.showMessage("Connecting to Binance...")

        # Table polished (create before panel so we can set hidden state)
        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(self.columns)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultSectionSize(100)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("""
            QTableWidget {
                gridline-color: #ccc;
                font-size: 12px;
            }
            QHeaderView::section {
                background-color: #eee;
                font-weight: bold;
                padding: 4px;
                border: 1px solid #ccc;
            }
        """)

        # allow right-click on header to toggle panel visibility
        header = self.table.horizontalHeader()
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_context_menu)

        # Column panel (fixed, nice-looking)
        self.col_panel = QGroupBox("Columns")
        self.col_panel.setMinimumWidth(220)
        self.col_panel.setMaximumWidth(280)
        self.col_panel_layout = QVBoxLayout()
        self.col_panel_layout.setContentsMargins(8, 8, 8, 8)
        self.col_checkboxes = []

        # load saved state (name -> bool) (may also contain panel visibility under "__panel_visible__")
        self._loaded_column_state = self._load_column_state()

        for idx, name in enumerate(self.columns):
            checked = self._loaded_column_state.get(name, True)
            cb = QCheckBox(name)
            cb.setChecked(checked)
            # connect with index captured
            cb.toggled.connect(lambda checked, col=idx: self.set_column_visible(col, checked))
            self.col_checkboxes.append(cb)
            self.col_panel_layout.addWidget(cb)

        # buttons show/hide all
        btns = QWidget()
        btns_l = QHBoxLayout()
        btns_l.setContentsMargins(0, 0, 0, 0)
        self.show_all_btn = QPushButton("Show all")
        self.hide_all_btn = QPushButton("Hide all")
        self.show_all_btn.clicked.connect(self._show_all_columns)
        self.hide_all_btn.clicked.connect(self._hide_all_columns)
        btns_l.addWidget(self.show_all_btn)
        btns_l.addWidget(self.hide_all_btn)
        btns.setLayout(btns_l)
        self.col_panel_layout.addWidget(btns)

        # small spacer
        spacer = QFrame()
        spacer.setFrameShape(QFrame.HLine)
        self.col_panel_layout.addWidget(spacer)

        # helpful hint
        hint = QLabel("Tip: right-click header to toggle panel")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        self.col_panel_layout.addWidget(hint)

        self.col_panel.setLayout(self.col_panel_layout)
        self.col_panel.setStyleSheet("""
            QGroupBox {
                border: 1px solid #ddd;
                border-radius: 6px;
                margin-top: 6px;
                padding: 6px;
                background: #fafafa;
            }
        """)

        # panel default width used for animation
        self._panel_default_width = 260

        # apply loaded states to actual table columns
        for idx, name in enumerate(self.columns):
            visible = self._loaded_column_state.get(name, True)
            self.table.setColumnHidden(idx, not visible)

        # apply loaded panel visibility (persisted)
        panel_visible = self._loaded_column_state.get("__panel_visible__", True)
        self.col_panel.setVisible(panel_visible)
        if not panel_visible:
            # ensure maximum width is 0 so layout doesn't reserve space
            self.col_panel.setMaximumWidth(0)
        else:
            self.col_panel.setMaximumWidth(self.col_panel.maximumWidth() or self._panel_default_width)

        # Toggle button for the panel (added to controls area)
        self.toggle_panel_btn = QPushButton("Hide panel" if panel_visible else "Show panel")
        self.toggle_panel_btn.setFixedWidth(110)
        self.toggle_panel_btn.clicked.connect(self._on_toggle_panel_btn)
        controls_layout.addWidget(self.toggle_panel_btn)

        # assemble right side: controls + lower area (panel + table)
        lower_area = QHBoxLayout()
        lower_area.addWidget(self.col_panel)
        lower_area.addWidget(self.table, stretch=1)

        right_layout.addWidget(controls)
        right_layout.addLayout(lower_area)
        right_widget.setLayout(right_layout)

        splitter.addWidget(right_widget)
        self.setCentralWidget(splitter)

        # populate table initially
        self.refresh_table()

        # keep a ref to current animation to avoid GC
        self._panel_anim = None

        # start websocket worker
        self.worker = KlineWorker(symbol="solusdt", interval="1m")
        self.worker.kline_received.connect(self.on_kline)
        self.worker.error.connect(self.on_ws_error)
        self.worker.start()

    def closeEvent(self, event):
        # save column & panel state
        self._save_column_state()
        # stop worker cleanly
        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
        event.accept()

    @Slot(str)
    def on_ws_error(self, msg):
        print("WebSocket error:", msg)  # you can also show in UI

    @Slot(dict)
    def on_kline(self, payload):
        # (same as before)
        open_time = payload["open_time"]
        close_time = payload["close_time"]

        if (self.df["open_time"] == open_time).any():
            idx = self.df.index[self.df["open_time"] == open_time][0]
            self.df.at[idx, "open"] = payload["open"]
            self.df.at[idx, "high"] = payload["high"]
            self.df.at[idx, "low"] = payload["low"]
            self.df.at[idx, "close"] = payload["close"]
            self.df.at[idx, "volume"] = payload["volume"]
            self.df.at[idx, "close_time"] = payload["close_time"]
        else:
            new_row = {
                "open_time": open_time,
                "close_time": close_time,
                "open": payload["open"],
                "high": payload["high"],
                "low": payload["low"],
                "close": payload["close"],
                "volume": payload["volume"],
            }
            self.df = pd.concat([self.df, pd.DataFrame([new_row])], ignore_index=True)
            if len(self.df) > 200:
                self.df = self.df.iloc[-200:].reset_index(drop=True)

        # Recompute time columns and indicators for entire df
        self.df["open_time"] = self.df["open_time"].astype(int)
        self.df["close_time"] = self.df["close_time"].astype(int)
        self.df["open_dt"] = pd.to_datetime(self.df["open_time"], unit="ms", utc=True).dt.tz_convert("Asia/Ho_Chi_Minh")
        self.df["close_dt"] = pd.to_datetime(self.df["close_time"], unit="ms", utc=True).dt.tz_convert("Asia/Ho_Chi_Minh")
        self.df["Timeframe"] = self.df["open_dt"].dt.strftime("%H:%M:%S") + " - " + self.df["close_dt"].dt.strftime("%H:%M:%S")

        # Indicators
        self.df["RSI"] = ta.rsi(self.df["close"], length=14)
        self.df["EMA9"] = ta.ema(self.df["close"], length=9)
        self.df["EMA21"] = ta.ema(self.df["close"], length=21)
        self.df["Signal"] = self.df.apply(calc_signal, axis=1)

        # Update price label with color
        last_close = float(self.df["close"].iloc[-1])
        prev_close = float(self.df["close"].iloc[-2]) if len(self.df) > 1 else last_close
        self.price_label.setText(f"Current Price: {last_close:.6f} USDT")

        if last_close > prev_close:
            self.price_label.setStyleSheet("color: green;")
        elif last_close < prev_close:
            self.price_label.setStyleSheet("color: red;")
        else:
            self.price_label.setStyleSheet("color: black;")

        # Update status bar
        now_str = datetime.now().astimezone().strftime("%H:%M:%S")
        self.status.showMessage(f"Last update: {now_str} | Connected ✅")

        self.refresh_table()

    def refresh_table(self):
        # newest on top
        df = self.df.copy().sort_values("open_time", ascending=False).reset_index(drop=True)

        self.table.setRowCount(len(df))
        for i, row in df.iterrows():
            vals = [
                row.get("Timeframe", ""),
                f"{row.get('open', 0):.6f}",
                f"{row.get('high', 0):.6f}",
                f"{row.get('low', 0):.6f}",
                f"{row.get('close', 0):.6f}",
                f"{row.get('volume', 0):.6f}",
                f"{row.get('RSI'):.2f}" if pd.notna(row.get("RSI")) else "",
                f"{row.get('EMA9'):.6f}" if pd.notna(row.get("EMA9")) else "",
                f"{row.get('EMA21'):.6f}" if pd.notna(row.get("EMA21")) else "",
                row.get("Signal", ""),
                "Yes" if row.get("close_time") and not pd.isna(row.get("close_time")) else ""
            ]
            for j, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                # Align numbers to right for readability
                if j in (1,2,3,4,5,6,7,8):
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
                self.table.setItem(i, j, item)

            # color row based on signal
            signal = row.get("Signal", "")
            if signal == "Long":
                color = QColor(200, 255, 200)
            elif signal == "Short":
                color = QColor(255, 200, 200)
            elif signal == "Neutral":
                color = QColor(220, 220, 220)
            else:
                color = QColor(255, 255, 255)
            for j in range(self.table.columnCount()):
                it = self.table.item(i, j)
                if it:
                    it.setBackground(color)

    # ------------------------
    # Column panel helpers & state persistence
    # ------------------------
    def set_column_visible(self, col_idx, visible):
        # update table column visibility and save state
        self.table.setColumnHidden(col_idx, not visible)
        # keep checkbox state consistent (in case external calls)
        try:
            cb = self.col_checkboxes[col_idx]
            if cb.isChecked() != visible:
                cb.setChecked(visible)
        except Exception:
            pass
        self._save_column_state()

    def _show_all_columns(self):
        for idx, cb in enumerate(self.col_checkboxes):
            cb.setChecked(True)
            self.table.setColumnHidden(idx, False)
        self._save_column_state()

    def _hide_all_columns(self):
        for idx, cb in enumerate(self.col_checkboxes):
            cb.setChecked(False)
            self.table.setColumnHidden(idx, True)
        self._save_column_state()

    def _on_header_context_menu(self, pos):
        # toggle visibility of the fixed panel (use same animated toggle)
        self.toggle_col_panel()

    def _on_toggle_panel_btn(self):
        self.toggle_col_panel()

    def toggle_col_panel(self):
        """Animate & toggle the column panel. Persist state after change."""
        if self.col_panel.isVisible():
            # hide with animation
            current_w = self.col_panel.width() or self._panel_default_width
            self._panel_default_width = current_w  # remember for reopen
            anim = QPropertyAnimation(self.col_panel, b"maximumWidth", self)
            anim.setDuration(220)
            anim.setStartValue(current_w)
            anim.setEndValue(0)
            anim.setEasingCurve(QEasingCurve.InOutCubic)

            def finish_hide():
                # fully hide and restore maximumWidth to default for future show
                self.col_panel.setVisible(False)
                self.col_panel.setMaximumWidth(self._panel_default_width)
                self.toggle_panel_btn.setText("Show panel")
                self._save_column_state()

            anim.finished.connect(finish_hide)
            anim.start()
            self._panel_anim = anim
        else:
            # show with animation
            self.col_panel.setVisible(True)
            # start from 0 width
            self.col_panel.setMaximumWidth(0)
            anim = QPropertyAnimation(self.col_panel, b"maximumWidth", self)
            anim.setDuration(220)
            anim.setStartValue(0)
            anim.setEndValue(self._panel_default_width)
            anim.setEasingCurve(QEasingCurve.InOutCubic)

            def finish_show():
                # ensure maximum width back to allowed max (let layout handle it)
                self.col_panel.setMaximumWidth(self.col_panel.maximumWidth() or self._panel_default_width)
                self.toggle_panel_btn.setText("Hide panel")
                self._save_column_state()

            anim.finished.connect(finish_show)
            anim.start()
            self._panel_anim = anim

    def _state_file_path(self):
        # try platform app config location
        try:
            loc = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
            if not loc:
                loc = os.path.expanduser("~")
        except Exception:
            loc = os.path.expanduser("~")
        p = Path(loc)
        p.mkdir(parents=True, exist_ok=True)
        return p / "solusdt_columns.json"

    def _load_column_state(self):
        path = self._state_file_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # ensure keys are column names; values boolean
                if isinstance(data, dict):
                    return {k: bool(v) for k, v in data.items()}
            except Exception as e:
                print("Failed to load column state:", e)
        return {}

    def _save_column_state(self):
        data = {name: (not self.table.isColumnHidden(idx)) for idx, name in enumerate(self.columns)}
        # include panel visibility
        data["__panel_visible__"] = bool(self.col_panel.isVisible())
        path = self._state_file_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print("Failed to save column state:", e)


# ------------------------
# Run app
# ------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
