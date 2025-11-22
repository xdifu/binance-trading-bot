import os
import sys
from dotenv import load_dotenv

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from binance_api.client import BinanceClient
import config

# Load env vars
load_dotenv()

def check_orders():
    print(f"Checking open orders for {config.SYMBOL}...")
    print(f"API Key present: {bool(config.API_KEY)}")
    print(f"Testnet: {config.USE_TESTNET}")
    
    client = BinanceClient()
    try:
        orders = client.get_open_orders(config.SYMBOL)
        print(f"Found {len(orders)} open orders:")
        for order in orders:
            print(f"- ID: {order['orderId']}, Side: {order['side']}, Price: {order['price']}, OrigQty: {order['origQty']}, ListId: {order.get('orderListId')}")
    except Exception as e:
        print(f"Error fetching orders: {e}")

if __name__ == "__main__":
    check_orders()
