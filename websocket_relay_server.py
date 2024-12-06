from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
import json
from typing import Set
import socketio
import logging

# ロギングの設定
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 開発環境用。本番環境では具体的なオリジンを指定する
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_websockets=True  # WebSocket接続を明示的に許可
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()

    async def connect(self, websocket: WebSocket) -> None:
        try:
            await websocket.accept()
            async with self.lock:
                self.active_connections.add(websocket)
            logger.info(f"Client connected. Total connections: {len(self.active_connections)}")
        except Exception as e:
            logger.error(f"Error in connect: {e}")
            raise

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self.lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        logger.info(f"Client disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, data: dict) -> None:
        if not self.active_connections:
            return

        async with self.lock:
            for connection in list(self.active_connections):
                try:
                    await connection.send_json(data)
                except Exception as e:
                    logger.error(f"Error broadcasting to client: {e}")
                    await self.disconnect(connection)

manager = ConnectionManager()

class BitflyerClient:
    def __init__(self):
        self.sio = socketio.AsyncClient(logger=True, engineio_logger=True)
        self.latest_data = {
            "mid_price": 0,
            "asks": [],
            "bids": []
        }
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.setup_handlers()

    def setup_handlers(self):
        @self.sio.event
        async def connect():
            logger.info("Connected to BitFlyer")
            await self.subscribe_channels()
            self.ready.set()

        @self.sio.event
        async def connect_error(data):
            logger.error(f"Connection error to BitFlyer: {data}")
            self.ready.clear()

        @self.sio.event
        async def disconnect():
            logger.warning("Disconnected from BitFlyer")
            self.ready.clear()

    async def subscribe_channels(self):
        try:
            channels = ["lightning_board_snapshot_BTC_JPY", "lightning_board_BTC_JPY"]
            for channel in channels:
                await self.sio.emit("subscribe", channel)
                logger.info(f"Subscribed to {channel}")
        except Exception as e:
            logger.error(f"Error subscribing to channels: {e}")
    async def connect_with_retry(self):
        while True:
            try:
                if not self.sio.connected:
                    logger.info("Attempting to connect to BitFlyer...")
                    await self.sio.connect('https://io.lightstream.bitflyer.com', 
                                         transports=['websocket'],
                                         wait_timeout=10)
                    await self.ready.wait()
                return
            except Exception as e:
                logger.error(f"Failed to connect to BitFlyer: {e}")
                await asyncio.sleep(5)

    async def handle_board_data(self, data, is_snapshot=False):
        async with self.lock:
            try:
                if is_snapshot:
                    self.latest_data = {
                        "mid_price": float(data.get("mid_price", 0)),
                        "asks": sorted(data.get("asks", []), 
                                     key=lambda x: float(x["price"]))[:10],
                        "bids": sorted(data.get("bids", []), 
                                     key=lambda x: float(x["price"]), 
                                     reverse=True)[:10]
                    }
                else:
                    if "mid_price" in data:
                        self.latest_data["mid_price"] = float(data["mid_price"])
                
                await manager.broadcast(self.latest_data)
                logger.debug(f"Broadcasted data: {self.latest_data}")
            except Exception as e:
                logger.error(f"Error handling board data: {e}")

bitflyer_client = BitflyerClient()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Origin チェックをスキップ
    await websocket.accept()
    print("WebSocket connection accepted")
    
    try:
        while True:
            data = await websocket.receive_text()
            print(f"Received: {data}")
    except WebSocketDisconnect:
        print("Client disconnected")

@app.on_event("startup")
async def startup_event():
    logger.info("Starting server...")
    await bitflyer_client.connect_with_retry()

if __name__ == "__main__":
    uvicorn.run(app, 
                host="127.0.0.1", 
                port=8000, 
                log_level="debug",
                access_log=True)