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
    QHBoxLayout, QSizePolicy
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor

# pyqtgraph for sparkline
import pyqtgraph as pg

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
        self.df = fetch_ohlcv(limit=100)

        # track which row is selected in the table
        self.selected_row = None

        # layout
        splitter = QSplitter(Qt.Horizontal)

        # left: web view
        self.webview = QWebEngineView()
        self.webview.setUrl("https://www.binance.com/en/futures/SOLUSDT")
        splitter.addWidget(self.webview)

        # -------------------------------------------------
        # Right: Master / Detail layout
        # -------------------------------------------------
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        # --- Summary row ---
        summary_widget = QWidget()
        summary_layout = QHBoxLayout(summary_widget)

        # price label
        self.price_label = QLabel("Current Price: ...")
        font = self.price_label.font()
        font.setPointSize(16)
        font.setBold(True)
        self.price_label.setFont(font)
        summary_layout.addWidget(self.price_label)

        # RSI + EMA labels
        self.rsi_label = QLabel("RSI: --")
        self.ema_label = QLabel("EMA9/EMA21: -- / --")
        summary_layout.addWidget(self.rsi_label)
        summary_layout.addWidget(self.ema_label)
        summary_layout.addStretch()

        right_layout.addWidget(summary_widget)

        # --- Splitter cho bảng (master) và panel chi tiết ---
        detail_splitter = QSplitter(Qt.Horizontal)

        # Compact table (master)
        self.compact_table = QTableWidget(0, 6)
        self.compact_table.setHorizontalHeaderLabels(
            ["Timeframe", "Close", "Δ%", "Volume", "RSI", "Signal"]
        )
        self.compact_table.horizontalHeader().setStretchLastSection(True)
        self.compact_table.verticalHeader().setVisible(False)
        self.compact_table.setAlternatingRowColors(True)
        self.compact_table.setStyleSheet("""
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
        detail_splitter.addWidget(self.compact_table)

        # Detail panel (chi tiết row được chọn)
        self.detail_panel = QWidget()
        d_layout = QVBoxLayout(self.detail_panel)
        self.detail_info = QLabel("Select a row to see details...")
        self.detail_info.setWordWrap(True)
        d_layout.addWidget(self.detail_info)

        # --- sparkline / mini chart (pyqtgraph) ---
        # Create PlotWidget and style it as a compact sparkline
        self.detail_plot = pg.PlotWidget()
        self.detail_plot.setMinimumHeight(140)
        self.detail_plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Minimal appearance for sparkline: hide axes and disable mouse
        pl_item = self.detail_plot.getPlotItem()
        pl_item.hideAxis('bottom')
        pl_item.hideAxis('left')
        pl_item.setMenuEnabled(False)
        self.detail_plot.setMouseEnabled(x=False, y=False)

        # Optionally remove background grid to be sparkline-like
        pl_item.showGrid(x=False, y=False)

        d_layout.addWidget(self.detail_plot)

        detail_splitter.addWidget(self.detail_panel)

        right_layout.addWidget(detail_splitter)

        splitter.addWidget(right_widget)
        self.setCentralWidget(splitter)

        # Status bar
        self.status = self.statusBar()
        self.status.showMessage("Connecting to Binance...")

        # populate table initially
        self.refresh_table()

        # start websocket worker
        self.worker = KlineWorker(symbol="solusdt", interval="1m")
        self.worker.kline_received.connect(self.on_kline)
        self.worker.error.connect(self.on_ws_error)
        self.worker.start()

        # connect table row selection
        self.compact_table.cellClicked.connect(self.on_row_selected)

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

        # Nếu có hàng được chọn, cập nhật lại sparkline (use same index if still valid)
        if self.selected_row is not None:
            # clamp selected_row into new df range
            sel = min(self.selected_row, len(self.df) - 1)
            self.plot_sparkline(sel)

    def refresh_table(self):
        df = self.df.copy().reset_index(drop=True)
        self.compact_table.setRowCount(len(df))

        for i, row in df.iterrows():
            # Change %
            change_pct = ""
            if i > 0 and pd.notna(row.get("close")):
                prev = df.iloc[i-1].get("close")
                if pd.notna(prev) and prev != 0:
                    change_pct = f"{(row['close']/prev - 1)*100:.2f}%"

            vals = [
                row.get("Timeframe", ""),
                f"{row.get('close', 0):.6f}",
                change_pct,
                f"{row.get('volume', 0):.2f}",
                f"{row.get('RSI'):.2f}" if pd.notna(row.get("RSI")) else "",
                row.get("Signal", "")
            ]

            for j, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if j in (1,2,3,4):  # align numbers
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
                self.compact_table.setItem(i, j, item)

            # màu dòng theo Signal
            signal = row.get("Signal", "")
            if signal == "Long":
                color = QColor(200, 255, 200)
            elif signal == "Short":
                color = QColor(255, 200, 200)
            elif signal == "Neutral":
                color = QColor(220, 220, 220)
            else:
                color = QColor(255, 255, 255)
            for j in range(self.compact_table.columnCount()):
                it = self.compact_table.item(i, j)
                if it:
                    it.setBackground(color)

        # cập nhật summary (RSI, EMA)
        if not df.empty:
            last_rsi = df["RSI"].iloc[-1]
            last_ema9 = df["EMA9"].iloc[-1]
            last_ema21 = df["EMA21"].iloc[-1]
            self.rsi_label.setText(f"RSI: {last_rsi:.2f}" if pd.notna(last_rsi) else "RSI: --")
            if pd.notna(last_ema9) and pd.notna(last_ema21):
                self.ema_label.setText(f"EMA9/EMA21: {last_ema9:.2f} / {last_ema21:.2f}")
            else:
                self.ema_label.setText("EMA9/EMA21: -- / --")

    def on_row_selected(self, row, col):
        if row >= len(self.df):
            return
        self.selected_row = row  # remember selection
        r = self.df.iloc[row]
        detail_text = (
            f"Time: {r.get('Timeframe','')}\n"
            f"Open: {r.get('open',0):.6f}\n"
            f"High: {r.get('high',0):.6f}\n"
            f"Low: {r.get('low',0):.6f}\n"
            f"Close: {r.get('close',0):.6f}\n"
            f"Volume: {r.get('volume',0):.2f}\n"
            f"RSI: {r.get('RSI', float('nan')):.2f}\n"
            f"EMA9: {r.get('EMA9', float('nan')):.2f}\n"
            f"EMA21: {r.get('EMA21', float('nan')):.2f}\n"
            f"Signal: {r.get('Signal','')}"
        )
        self.detail_info.setText(detail_text)

        # draw sparkline for this row
        self.plot_sparkline(row)

    def plot_sparkline(self, row_index, window=30):
        """
        Vẽ sparkline: lấy tối đa `window` giá close trước và bao gồm row_index.
        row_index: int (index in self.df)
        """
        if self.df is None or len(self.df) == 0:
            return
        # clamp
        row_index = int(max(0, min(row_index, len(self.df) - 1)))
        start = max(0, row_index - window + 1)
        closes = self.df["close"].iloc[start:row_index+1].astype(float).to_numpy()

        if len(closes) == 0:
            self.detail_plot.clear()
            return

        self.detail_plot.clear()

        # Decide pen color by last vs first (green if up, red if down)
        if closes[-1] >= closes[0]:
            pen = pg.mkPen(color=(0, 180, 0), width=2)
        else:
            pen = pg.mkPen(color=(200, 0, 0), width=2)

        # Plot line
        x = list(range(len(closes)))
        self.detail_plot.plot(x, closes, pen=pen, antialias=True)

        # Highlight last point with a symbol
        last_symbol = pg.ScatterPlotItem([x[-1]], [closes[-1]], size=8, brush=pen.color(), pen=pg.mkPen('w', width=1))
        self.detail_plot.addItem(last_symbol)

        # Optionally draw a faint fill under the curve
        # (pyqtgraph doesn't have direct filled curve, we can approximate with a FillBetweenItem if needed)
        # For simplicity we skip complex fill; keep sparkline minimal.

# ------------------------
# Run app
# ------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
