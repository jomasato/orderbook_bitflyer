from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# セキュリティ設定を緩和
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # すべてのオリジンを許可
    allow_methods=["*"],  # すべてのメソッドを許可
    allow_headers=["*"],  # すべてのヘッダーを許可
    allow_credentials=True,  # 重要: クレデンシャルを許可
)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    print("Attempting to accept connection...")
    await websocket.accept()
    print("Connection accepted!")
    
    try:
        while True:
            data = await websocket.receive_text()
            print(f"Message received: {data}")
            await websocket.send_text(f"Message text was: {data}")
    except Exception as e:
        print(f"Error occurred: {e}")

# 基本的なHTTPエンドポイントも追加して疎通確認
@app.get("/")
async def root():
    return {"message": "Hello World"}