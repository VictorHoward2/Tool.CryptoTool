import sys
import json
import asyncio
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QLabel, QTableWidget, QTableWidgetItem, QSplitter,
    QToolButton, QHBoxLayout, QMenu
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import Qt, QThread, Signal, Slot, QUrl
from PySide6.QtGui import QColor, QAction

# NOTE: websockets is an asyncio library
import websockets

# ------------------------
# Helper / REST fetch
# ------------------------
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
# Signal calculation
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

        # columns (used both for header labels and menu)
        self.columns = [
            "Timeframe", "Open", "High", "Low", "Close", "Volume",
            "RSI", "EMA9", "EMA21", "Signal", "Closed?"
        ]

        # layout
        splitter = QSplitter(Qt.Horizontal)

        # left: web view
        self.webview = QWebEngineView()
        # setUrl expects QUrl
        try:
            self.webview.setUrl(QUrl("https://www.binance.com/en/futures/SOLUSDT"))
        except Exception:
            # fallback if earlier version accepted string
            self.webview.setUrl("https://www.binance.com/en/futures/SOLUSDT")
        splitter.addWidget(self.webview)

        # right: indicators
        right_widget = QWidget()
        right_layout = QVBoxLayout()
        
        # Price label polished
        self.price_label = QLabel("Current Price: ...")
        self.price_label.setAlignment(Qt.AlignCenter)
        font = self.price_label.font()
        font.setPointSize(16)
        font.setBold(True)
        self.price_label.setFont(font)

        # Control bar (price + columns button)
        controls = QWidget()
        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self.price_label)

        # Column toggle toolbutton
        self.col_btn = QToolButton()
        self.col_btn.setText("Columns ▾")
        self.col_menu = self._create_column_menu()
        self.col_btn.setMenu(self.col_menu)
        # InstantPopup so clicking shows the menu immediately
        self.col_btn.setPopupMode(QToolButton.InstantPopup)
        controls_layout.addWidget(self.col_btn)
        controls.setLayout(controls_layout)

        # Status bar
        self.status = self.statusBar()
        self.status.showMessage("Connecting to Binance...")

        # Table polished
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

        # allow right-click on header to open column menu
        header = self.table.horizontalHeader()
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_context_menu)

        right_layout.addWidget(controls)
        right_layout.addWidget(self.table)
        right_widget.setLayout(right_layout)

        splitter.addWidget(right_widget)
        self.setCentralWidget(splitter)

        # populate table initially
        self.refresh_table()

        # start websocket worker
        self.worker = KlineWorker(symbol="solusdt", interval="1m")
        self.worker.kline_received.connect(self.on_kline)
        self.worker.error.connect(self.on_ws_error)
        self.worker.start()

    def closeEvent(self, event):
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
        """
        Payload example:
        {
          "open_time": 169xxx..., "close_time": 169xxx..., "open": 1.23,
          "high": 1.25, "low": 1.20, "close": 1.24, "volume": 123.4, "is_closed": False
        }
        """
        # Convert to df row-like
        open_time = payload["open_time"]
        close_time = payload["close_time"]

        # If open_time exists in df, replace that row, otherwise append
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
            # keep last N rows
            if len(self.df) > 200:
                self.df = self.df.iloc[-200:].reset_index(drop=True)

        # Recompute time columns and indicators for entire df (could be optimized)
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

        # Update price label với màu
        last_close = float(self.df["close"].iloc[-1])
        prev_close = float(self.df["close"].iloc[-2]) if len(self.df) > 1 else last_close
        self.price_label.setText(f"Current Price: {last_close:.6f} USDT")

        if last_close > prev_close:
            self.price_label.setStyleSheet("color: green;")
        elif last_close < prev_close:
            self.price_label.setStyleSheet("color: red;")
        else:
            self.price_label.setStyleSheet("color: black;")

        # Cập nhật status bar
        now_str = datetime.now().astimezone().strftime("%H:%M:%S")
        self.status.showMessage(f"Last update: {now_str} | Connected ✅")

        self.refresh_table()

    def refresh_table(self):
        # Hiển thị: mới nhất ở trên -> sắp xếp open_time giảm dần khi render
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
    # Column toggle menu helpers
    # ------------------------
    def _create_column_menu(self):
        menu = QMenu(self)
        self.col_actions = []

        for idx, name in enumerate(self.columns):
            act = QAction(name, self)
            act.setCheckable(True)
            act.setChecked(True)  # default show
            # when toggled, hide column if unchecked
            act.toggled.connect(lambda checked, col=idx: self.table.setColumnHidden(col, not checked))
            menu.addAction(act)
            self.col_actions.append(act)

        menu.addSeparator()
        show_all_act = QAction("Show all", self)
        hide_all_act = QAction("Hide all", self)
        show_all_act.triggered.connect(self._show_all_columns)
        hide_all_act.triggered.connect(self._hide_all_columns)
        menu.addAction(show_all_act)
        menu.addAction(hide_all_act)

        return menu

    def _show_all_columns(self):
        for idx, act in enumerate(self.col_actions):
            act.setChecked(True)
            self.table.setColumnHidden(idx, False)

    def _hide_all_columns(self):
        for idx, act in enumerate(self.col_actions):
            act.setChecked(False)
            self.table.setColumnHidden(idx, True)

    def _on_header_context_menu(self, pos):
        header = self.table.horizontalHeader()
        # show the same column menu at mouse position
        self.col_menu.exec_(header.mapToGlobal(pos))


# ------------------------
# Run app
# ------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
