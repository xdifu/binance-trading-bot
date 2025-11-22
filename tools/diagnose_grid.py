import os
import sys
from dotenv import load_dotenv

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from binance_api.client import BinanceClient
from core.grid_trader import GridTrader
from tg_bot.bot import TelegramBot
import config

# Load env vars
load_dotenv()

def diagnose_grid():
    print("=" * 80)
    print("GRID STRUCTURE DIAGNOSTIC REPORT")
    print("=" * 80)
    
    # Initialize components
    client = BinanceClient()
    
    # Initialize Telegram bot (may be None if not configured)
    telegram_bot = None
    try:
        if config.TELEGRAM_TOKEN and config.ALLOWED_TELEGRAM_USERS:
            telegram_bot = TelegramBot(
                token=config.TELEGRAM_TOKEN,
                allowed_users=config.ALLOWED_TELEGRAM_USERS
            )
    except:
        pass
    
    trader = GridTrader(binance_client=client, telegram_bot=telegram_bot)
    
    # Check if grid is initialized
    if not hasattr(trader, 'grid') or not trader.grid:
        print("\n⚠️  Grid is not initialized. Calculating grid levels...")
        trader._setup_grid()
    
    print(f"\n📊 Grid Configuration:")
    print(f"   Symbol: {config.SYMBOL}")
    print(f"   Capital Per Level: {config.CAPITAL_PER_LEVEL} USDT")
    print(f"   Configured Grid Levels: {config.GRID_LEVELS}")
    print(f"   Actual Grid Length: {len(trader.grid)}")
    
    print(f"\n🔍 Detailed Grid Structure:")
    print(f"{'Index':<6} {'Side':<6} {'Price':<15} {'Order ID':<15} {'Capital':<10}")
    print("-" * 70)
    
    price_counts = {}
    for i, level in enumerate(trader.grid):
        price = level.get('price', 0)
        side = level.get('side', 'N/A')
        order_id = level.get('order_id', None)
        capital = level.get('capital', 0)
        
        # Track price duplicates
        price_key = f"{side}_{price:.8f}"
        price_counts[price_key] = price_counts.get(price_key, 0) + 1
        
        order_id_str = str(order_id) if order_id else "None"
        print(f"{i:<6} {side:<6} {price:<15.8f} {order_id_str:<15} {capital:<10.2f}")
    
    # Detect duplicates
    duplicates = {k: v for k, v in price_counts.items() if v > 1}
    if duplicates:
        print(f"\n⚠️  DUPLICATE PRICES DETECTED:")
        for price_key, count in duplicates.items():
            print(f"   {price_key}: appears {count} times")
    else:
        print(f"\n✅ No duplicate prices in grid structure")
    
    # Count orders with IDs
    orders_with_id = sum(1 for level in trader.grid if level.get('order_id'))
    print(f"\n📈 Order Status:")
    print(f"   Grid levels with order_id: {orders_with_id}/{len(trader.grid)}")
    
    # Get actual orders from exchange
    print(f"\n🌐 Exchange Orders:")
    try:
        orders = client.get_open_orders(config.SYMBOL)
        print(f"   Total open orders on exchange: {len(orders)}")
        
        # Match orders to grid levels
        matched = 0
        unmatched_orders = []
        for order in orders:
            found = False
            for level in trader.grid:
                if level.get('order_id') == order['orderId']:
                    matched += 1
                    found = True
                    break
            if not found:
                unmatched_orders.append(order)
        
        print(f"   Orders matched to grid: {matched}")
        print(f"   Orphan orders (not in grid): {len(unmatched_orders)}")
        
        if unmatched_orders:
            print(f"\n⚠️  ORPHAN ORDERS:")
            for order in unmatched_orders:
                print(f"   ID: {order['orderId']}, {order['side']} @ {order['price']}")
    except Exception as e:
        print(f"   Error fetching orders: {e}")
    
    print("\n" + "=" * 80)

if __name__ == "__main__":
    diagnose_grid()
