import asyncio
import time
import websocket
import json
import gzip
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional
from decimal import Decimal
from datetime import datetime, timezone
import logging
from threading import Lock

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

@dataclass
class OrderBookLevel:
    price: Decimal
    size: Decimal

@dataclass
class OrderBook:
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    timestamp: int

@dataclass
class ArbitrageOpportunity:
    buy_exchange: str
    sell_exchange: str
    layers: List[Dict[str, Decimal]]
    total_profit: Decimal
    total_volume: Decimal

class ArbitrageMonitor:
    def __init__(self, maker_fee: float = 0.0002, taker_fee: float = 0.0001):
        self.bittrade_orderbook: Optional[OrderBook] = None
        self.gmo_orderbook: Optional[OrderBook] = None
        self.maker_fee = Decimal(str(maker_fee))
        self.taker_fee = Decimal(str(taker_fee))
        self.lock = Lock()

class LayeredArbitrageMonitor(ArbitrageMonitor):
    def __init__(self, maker_fee: float = 0.0002, taker_fee: float = 0.0001, max_layers: int = 5):
        self.bittrade_orderbook: Optional[OrderBook] = None
        self.gmo_orderbook: Optional[OrderBook] = None
        self.maker_fee = Decimal(str(maker_fee))
        self.taker_fee = Decimal(str(taker_fee))
        self.lock = Lock()
        self.last_check_time = 0
        self.max_layers = max_layers

    def _check_arbitrage_opportunity(self):
        if not (self.bittrade_orderbook and self.gmo_orderbook):
            return

        # Bittrade買い + GMO売りの機会
        opportunity1 = self._analyze_layers(
            buy_orders=self.bittrade_orderbook.asks[:self.max_layers],
            sell_orders=self.gmo_orderbook.bids[:self.max_layers],
            buy_exchange="bittrade",
            sell_exchange="gmo"
        )

        # GMO買い + Bittrade売りの機会
        opportunity2 = self._analyze_layers(
            buy_orders=self.gmo_orderbook.asks[:self.max_layers],
            sell_orders=self.bittrade_orderbook.bids[:self.max_layers],
            buy_exchange="gmo",
            sell_exchange="bittrade"
        )

        if opportunity1 and opportunity1.total_profit > 0:
            self._log_opportunity(opportunity1)
        if opportunity2 and opportunity2.total_profit > 0:
            self._log_opportunity(opportunity2)

    def _analyze_layers(self, buy_orders: List[OrderBookLevel], 
                       sell_orders: List[OrderBookLevel],
                       buy_exchange: str, sell_exchange: str) -> Optional[ArbitrageOpportunity]:
        layers = []
        total_profit = Decimal('0')
        total_volume = Decimal('0')
        
        for i in range(min(len(buy_orders), len(sell_orders))):
            buy_price = buy_orders[i].price
            sell_price = sell_orders[i].price
            volume = min(buy_orders[i].size, sell_orders[i].size)
            
            # 手数料考慮後の実効価格を計算
            effective_buy_price = buy_price * (1 + self.maker_fee)
            effective_sell_price = sell_price * (1 - self.taker_fee)
            layer_profit = (effective_sell_price - effective_buy_price) * volume
            
            if layer_profit <= 0:
                break
                
            layers.append({
                'buy_price': buy_price,
                'sell_price': sell_price,
                'volume': volume,
                'profit': layer_profit
            })
            
            total_profit += layer_profit
            total_volume += volume

        if layers:
            return ArbitrageOpportunity(
                buy_exchange=buy_exchange,
                sell_exchange=sell_exchange,
                layers=layers,
                total_profit=total_profit,
                total_volume=total_volume
            )
        return None

    def _log_opportunity(self, opportunity: ArbitrageOpportunity):
        logging.info(
            f"\nArbitrage Opportunity Found:"
            f"\nBuy on {opportunity.buy_exchange} -> Sell on {opportunity.sell_exchange}"
            f"\nTotal Profit: {opportunity.total_profit} JPY"
            f"\nTotal Volume: {opportunity.total_volume} BTC"
            f"\nLayers:"
        )
        for i, layer in enumerate(opportunity.layers, 1):
            logging.info(
                f"Layer {i}: Buy @ {layer['buy_price']}, "
                f"Sell @ {layer['sell_price']}, "
                f"Volume: {layer['volume']}, "
                f"Profit: {layer['profit']} JPY"
            )


        
    def update_bittrade_orderbook(self, data: Dict):
        with self.lock:
            timestamp = data['ts']
            bids = [OrderBookLevel(Decimal(str(price)), Decimal(str(size))) 
                    for price, size in data['tick']['bids']]
            asks = [OrderBookLevel(Decimal(str(price)), Decimal(str(size))) 
                    for price, size in data['tick']['asks']]
            self.bittrade_orderbook = OrderBook(bids, asks, timestamp)
            self._try_check_arbitrage()

    def update_gmo_orderbook(self, data: Dict):
        with self.lock:
            timestamp = int(datetime.strptime(data['timestamp'], 
                                            '%Y-%m-%dT%H:%M:%S.%fZ')
                          .replace(tzinfo=timezone.utc).timestamp() * 1000)
            
            bids = [OrderBookLevel(Decimal(item['price']), Decimal(item['size'])) 
                    for item in data['bids']]
            asks = [OrderBookLevel(Decimal(item['price']), Decimal(item['size'])) 
                    for item in data['asks']]
            
            self.gmo_orderbook = OrderBook(bids, asks, timestamp)
            self._try_check_arbitrage()

    def _try_check_arbitrage(self):
        current_time = time.time() * 1000
        if current_time - self.last_check_time >= 100:  # 100ms以上経過している
            self._check_arbitrage_opportunity()
            self.last_check_time = current_time

    def _check_arbitrage_opportunity(self):
        if not (self.bittrade_orderbook and self.gmo_orderbook):
            return

        bittrade_best_bid = self.bittrade_orderbook.bids[0]
        bittrade_best_ask = self.bittrade_orderbook.asks[0]
        gmo_best_bid = self.gmo_orderbook.bids[0]
        gmo_best_ask = self.gmo_orderbook.asks[0]

        # 取引可能な数量を計算
        tradeable_volume_1 = min(bittrade_best_ask.size, gmo_best_bid.size)
        tradeable_volume_2 = min(gmo_best_ask.size, bittrade_best_bid.size)

        # Bittrade買い + GMO売りの機会をチェック
        spread1 = self._calculate_spread(
            buy_exchange="bittrade",
            sell_exchange="gmo",
            buy_price=bittrade_best_ask.price,
            sell_price=gmo_best_bid.price
        )

        # GMO買い + Bittrade売りの機会をチェック
        spread2 = self._calculate_spread(
            buy_exchange="gmo",
            sell_exchange="bittrade",
            buy_price=gmo_best_ask.price,
            sell_price=bittrade_best_bid.price
        )

        if spread1 > 0:
            logging.info(
                f"Opportunity found: Buy on Bittrade({bittrade_best_ask.price}) -> "
                f"Sell on GMO({gmo_best_bid.price}), "
                f"Spread: {spread1}, Volume: {tradeable_volume_1}"
            )
        elif spread2 > 0:
            logging.info(
                f"Opportunity found: Buy on GMO({gmo_best_ask.price}) -> "
                f"Sell on Bittrade({bittrade_best_bid.price}), "
                f"Spread: {spread2}, Volume: {tradeable_volume_2}"
            )

    def _calculate_spread(self, buy_exchange: str, sell_exchange: str, 
                         buy_price: Decimal, sell_price: Decimal) -> Decimal:
        buy_fee = buy_price * self.maker_fee
        sell_fee = sell_price * self.taker_fee
        
        effective_buy_price = buy_price + buy_fee
        effective_sell_price = sell_price - sell_fee
        
        spread = effective_sell_price - effective_buy_price
        return spread

class BittradeClient:
    def __init__(self, monitor: ArbitrageMonitor):
        self.monitor = monitor
        self.running = True
        self.ws = None
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_forever)
        self.thread.daemon = True
        self.thread.start()

    def _run_forever(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    "wss://api-cloud.bittrade.co.jp/ws",
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open
                )
                self.ws.run_forever()
                if self.running:
                    logging.info("Attempting to reconnect to Bittrade in 5 seconds...")
                    time.sleep(5)
            except Exception as e:
                logging.error(f"Bittrade connection error: {e}")
                time.sleep(5)

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()

    def _on_open(self, ws):
        logging.info("Bittrade WebSocket connected")
        subscription = {
            "sub": "market.xrpjpy.depth.step0",
            "id": "arbitrage_bot"
        }
        ws.send(json.dumps(subscription))

    def _on_message(self, ws, message):
        try:
            decompressed_data = gzip.decompress(message)
            data = json.loads(decompressed_data)
            
            if "ping" in data:
                pong_message = {"pong": data["ping"]}
                ws.send(json.dumps(pong_message))
                return
            
            if "tick" in data and "bids" in data["tick"]:
                self.monitor.update_bittrade_orderbook(data)
                #logging.info(f"Bittrade Orderbook Updated - Best Bid: {data['tick']['bids'][0][0]}, Best Ask: {data['tick']['asks'][0][0]}")
            
        except Exception as e:
            logging.error(f"Error processing Bittrade message: {e}")

    def _on_error(self, ws, error):
        logging.error(f"Bittrade WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logging.warning("Bittrade WebSocket connection closed")

class GmoClient:
    def __init__(self, monitor: ArbitrageMonitor):
        self.monitor = monitor
        self.ws = websocket.WebSocketApp(
            "wss://api.coin.z.com/ws/public/v1",
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.ws.run_forever)
        self.thread.daemon = True
        self.thread.start()

    def _on_open(self, ws):
        logging.info("GMO WebSocket connected")
        message = {
            "command": "subscribe",
            "channel": "orderbooks",
            "symbol": "XRP"
        }
        ws.send(json.dumps(message))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("channel") == "orderbooks":
                self.monitor.update_gmo_orderbook(data)
                #logging.info(f"GMO Orderbook Updated - Best Bid: {data['bids'][0]['price']}, Best Ask: {data['asks'][0]['price']}")
        except Exception as e:
            logging.error(f"Error processing GMO message: {e}")

    def _on_error(self, ws, error):
        logging.error(f"GMO WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logging.warning("GMO WebSocket connection closed")

def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    # 既存のArbitrageMonitorクラスを削除し、新しいLayeredArbitrageMonitorを使用
    monitor = LayeredArbitrageMonitor(maker_fee=0.0002, taker_fee=0.0001, max_layers=5)
    
    # 両取引所のクライアントを初期化
    bittrade_client = BittradeClient(monitor)
    gmo_client = GmoClient(monitor)

    # 両方のWebSocketを別スレッドで起動
    bittrade_client.start()
    gmo_client.start()

    try:
        # メインスレッドを維持
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down...")
        bittrade_client.ws.close()
        gmo_client.ws.close()
        
if __name__ == "__main__":
    main()
