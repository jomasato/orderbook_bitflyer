from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import socketio
import asyncio
import aiohttp
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class OrderBook:
    def __init__(self, product_code: str):
        self.product_code = product_code
        self.asks: Dict[float, float] = {}
        self.bids: Dict[float, float] = {}
        self.mid_price: float = 0
        self.last_sync: Optional[datetime] = None
        self.lock = asyncio.Lock()

    def clear(self):
        """板情報をクリアする"""
        self.asks.clear()
        self.bids.clear()
        self.mid_price = 0
        self.last_sync = None

    async def sync_from_http(self):
        async with self.lock:
            try:
                url = f'https://api.bitflyer.com/v1/board?product_code={self.product_code}'
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            self.update_from_snapshot(data)
                            self.last_sync = datetime.now()
                            logger.info(f"Successfully synced order book for {self.product_code}")
                            return True
                        else:
                            logger.error(f"HTTP sync failed for {self.product_code} with status {response.status}")
                            return False
            except Exception as e:
                logger.error(f"Error during HTTP sync for {self.product_code}: {e}")
                return False

    def update_from_snapshot(self, data: dict):
        self.mid_price = float(data.get("mid_price", 0))
        
        self.asks.clear()
        self.bids.clear()
        
        for ask in data.get("asks", []):
            self.asks[float(ask["price"])] = float(ask["size"])
        for bid in data.get("bids", []):
            self.bids[float(bid["price"])] = float(bid["size"])

    def update_from_board(self, data: dict):
        if "mid_price" in data:
            self.mid_price = float(data["mid_price"])
            
        for ask in data.get("asks", []):
            price = float(ask["price"])
            size = float(ask["size"])
            if size == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = size
                
        for bid in data.get("bids", []):
            price = float(bid["price"])
            size = float(bid["size"])
            if size == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = size

    def get_sorted_board(self, depth: int = 10) -> dict:
        sorted_asks = sorted(
            [{"price": price, "size": size} 
             for price, size in self.asks.items()],
            key=lambda x: x["price"]
        )[:depth]
        
        sorted_bids = sorted(
            [{"price": price, "size": size} 
             for price, size in self.bids.items()],
            key=lambda x: x["price"],
            reverse=True
        )[:depth]

        return {
            "product_code": self.product_code,
            "mid_price": self.mid_price,
            "asks": sorted_asks,
            "bids": sorted_bids,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None
        }

class OrderBookManager:
    def __init__(self):
        self.current_product: str = "BTC_JPY"
        self.order_books: Dict[str, OrderBook] = {}
        self.lock = asyncio.Lock()
        self.sio = socketio.AsyncClient()
        self.setup_socket_handlers()

    def setup_socket_handlers(self):
        @self.sio.event
        async def connect():
            logger.info("Connected to BitFlyer WebSocket")
            await self.subscribe_current_product()

        @self.sio.event
        async def disconnect():
            logger.warning("Disconnected from BitFlyer WebSocket")
            await self.sync_current_book()

        @self.sio.on("lightning_board_BTC_JPY")
        async def handle_btc_board(data):
            await self.handle_board_update("BTC_JPY", data)

        @self.sio.on("lightning_board_ETH_JPY")
        async def handle_eth_board(data):
            await self.handle_board_update("ETH_JPY", data)

    def get_current_book(self) -> Optional[OrderBook]:
        return self.order_books.get(self.current_product)

    async def change_product(self, product_code: str):
        if product_code == self.current_product:
            return

        async with self.lock:
            # 現在の銘柄の購読を解除
            if self.sio.connected:
                await self.sio.emit("unsubscribe", f"lightning_board_{self.current_product}")

            # 新しい銘柄の OrderBook を作成（存在しない場合）
            if product_code not in self.order_books:
                self.order_books[product_code] = OrderBook(product_code)
            
            # 現在の銘柄を切り替え
            self.current_product = product_code
            
            # 新しい銘柄の板情報を同期
            await self.sync_current_book()
            
            # 新しい銘柄の購読を開始
            if self.sio.connected:
                await self.subscribe_current_product()

    async def subscribe_current_product(self):
        await self.sio.emit("subscribe", f"lightning_board_{self.current_product}")
        logger.info(f"Subscribed to {self.current_product}")

    async def sync_current_book(self) -> bool:
        book = self.get_current_book()
        if book:
            return await book.sync_from_http()
        return False

    async def handle_board_update(self, product_code: str, data: dict):
        if product_code != self.current_product:
            return
        
        book = self.get_current_book()
        if book:
            book.update_from_board(data)
            # ここで broadcast を呼び出す必要があります

    async def start(self):
        # 初期の OrderBook を作成
        self.order_books[self.current_product] = OrderBook(self.current_product)
        await self.sync_current_book()
        
        retry_count = 0
        while True:
            try:
                await self.sio.connect('https://io.lightstream.bitflyer.com', 
                                     transports=['websocket'])
                break
            except Exception as e:
                retry_count += 1
                logger.error(f"Failed to connect to BitFlyer (attempt {retry_count}): {e}")
                await asyncio.sleep(min(30, 5 * retry_count))

    async def stop(self):
        if self.sio.connected:
            await self.sio.disconnect()

# FastAPI アプリケーションの設定
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# グローバルなインスタンス
manager = ConnectionManager()  # WebSocket接続管理用
book_manager = OrderBookManager()  # 板情報管理用

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # 接続時に現在の板情報を送信
        current_book = book_manager.get_current_book()
        if current_book:
            await websocket.send_json(current_book.get_sorted_board())
        
        while True:
            data = await websocket.receive_json()
            # 銘柄変更リクエストの処理
            if "change_product" in data:
                new_product = data["change_product"]
                await book_manager.change_product(new_product)
                # 新しい板情報をブロードキャスト
                current_book = book_manager.get_current_book()
                if current_book:
                    await manager.broadcast(current_book.get_sorted_board())
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await manager.disconnect(websocket)

@app.on_event("startup")
async def startup_event():
    await book_manager.start()

@app.on_event("shutdown")
async def shutdown_event():
    await book_manager.stop()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")