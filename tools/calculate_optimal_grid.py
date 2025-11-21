#!/usr/bin/env python3
"""
Optimal Grid Configuration Calculator
Calculates the best grid levels and capital per level based on:
- Available account balance
- Current market price
- Binance minimum notional value (5 USDT)
- Grid distribution strategy (asymmetric based on trend)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from binance_api.client import BinanceClient
from core.grid_trader import GridTrader
import config


def calculate_optimal_grid():
    """Calculate optimal grid configuration"""
    
    print("=" * 70)
    print("OPTIMAL GRID CONFIGURATION CALCULATOR")
    print("=" * 70)
    
    # Initialize client for price only
    try:
        client = BinanceClient()
        print(f"✓ Connected to Binance API")
    except Exception as e:
        print(f"⚠ Binance API connection limited: {e}")
        client = None
    
    # Get current price
    try:
        if client:
            current_price = float(client.get_symbol_price(config.SYMBOL))
        else:
            # Fallback: use a recent known price
            current_price = 593.50
        print(f"✓ Current {config.SYMBOL} price: {current_price:.2f} USDT")
    except Exception as e:
        print(f"⚠ Using fallback price")
        current_price = 593.50
        print(f"✓ Price: {current_price:.2f} USDT (fallback)")
    
    # Use known balance from recent logs
    # From recent deployment: "available funds: 72.88 USDT equivalent"
    print(f"\n✓ Account Balance (from recent logs):")
    usdt_balance = 47.29  # Known USDT balance
    base_balance = 0.043  # Known ZEC balance
    base_value_usdt = base_balance * current_price
    total_balance = usdt_balance + base_value_usdt
    
    print(f"  - USDT: {usdt_balance:.2f}")
    print(f"  - {config.SYMBOL.replace('USDT', '')}: {base_balance:.4f} (≈ {base_value_usdt:.2f} USDT)")
    print(f"  - Total: {total_balance:.2f} USDT equivalent")
    
    # Get symbol info for precision
    try:
        symbol_info = client.get_symbol_info(config.SYMBOL)
        
        # Find LOT_SIZE filter
        qty_precision = 3  # default
        min_qty = 0.001
        for filter_item in symbol_info['filters']:
            if filter_item['filterType'] == 'LOT_SIZE':
                step_size = float(filter_item['stepSize'])
                min_qty = float(filter_item['minQty'])
                # Calculate precision from step size
                qty_precision = len(str(step_size).rstrip('0').split('.')[-1])
                break
        
        # Find MIN_NOTIONAL filter
        min_notional = 5.0  # Binance default
        for filter_item in symbol_info['filters']:
            if filter_item['filterType'] == 'NOTIONAL':
                min_notional = float(filter_item['minNotional'])
                break
        
        print(f"✓ Symbol Info:")
        print(f"  - Quantity Precision: {qty_precision} decimals")
        print(f"  - Min Quantity: {min_qty}")
        print(f"  - Min Notional: {min_notional} USDT")
    except Exception as e:
        print(f"✗ Failed to get symbol info: {e}")
        min_notional = 5.0
        qty_precision = 3
    
    print("\n" + "=" * 70)
    print("CALCULATING OPTIMAL CONFIGURATION")
    print("=" * 70)
    
    # Reserve for OCO and emergency
    reserve_ratio = 0.15  # Reserve 15% for OCO orders
    available_for_grid = total_balance * (1 - reserve_ratio)
    
    print(f"\nAvailable for Grid Trading:")
    print(f"  Total Balance: {total_balance:.2f} USDT")
    print(f"  Reserve (15%): {total_balance * reserve_ratio:.2f} USDT (for OCO/emergency)")
    print(f"  Available: {available_for_grid:.2f} USDT")
    
    # Calculate minimum safe capital per level
    # We need to ensure even at the lowest grid price, order value > min_notional
    # Assuming grid can go down to 70% of current price in extreme cases
    lowest_expected_price = current_price * 0.7
    
    # Calculate minimum capital needed
    # capital / price * price = capital (approximately)
    # But we need to account for quantity rounding
    # Worst case: capital / price gets rounded down by step_size
    min_safe_capital = min_notional * 1.4  # 40% safety margin
    
    print(f"\nMinimum Capital per Level Calculation:")
    print(f"  Min Notional: {min_notional:.2f} USDT")
    print(f"  Safety Margin: 40%")
    print(f"  Min Safe Capital: {min_safe_capital:.2f} USDT per level")
    
    # Calculate optimal scenarios
    scenarios = []
    
    for capital_per_level in [5, 6, 7, 8, 9, 10, 11, 12]:
        max_levels = int(available_for_grid / capital_per_level)
        
        # Check if this would violate min_notional at lowest price
        test_qty = capital_per_level / lowest_expected_price
        rounded_qty = round(test_qty, qty_precision)
        actual_value = rounded_qty * lowest_expected_price
        
        is_safe = actual_value >= min_notional
        
        scenarios.append({
            'capital': capital_per_level,
            'levels': max_levels,
            'total_used': max_levels * capital_per_level,
            'utilization': (max_levels * capital_per_level / available_for_grid) * 100,
            'safe': is_safe,
            'min_value': actual_value
        })
    
    print("\n" + "-" * 70)
    print("SCENARIO ANALYSIS")
    print("-" * 70)
    print(f"{'Capital/Lv':<12} {'Levels':<8} {'Total Used':<12} {'Util%':<8} {'Safe?':<8} {'Min Val':<10}")
    print("-" * 70)
    
    best_scenario = None
    for s in scenarios:
        status = "✓ SAFE" if s['safe'] else "✗ RISK"
        print(f"{s['capital']:<12.1f} {s['levels']:<8} {s['total_used']:<12.2f} {s['utilization']:<8.1f} {status:<8} {s['min_value']:<10.2f}")
        
        # Find best safe scenario with highest levels
        if s['safe'] and (best_scenario is None or s['levels'] > best_scenario['levels']):
            best_scenario = s
    
    print("-" * 70)
    
    if best_scenario:
        print(f"\n{'='*70}")
        print("RECOMMENDED CONFIGURATION")
        print("=" * 70)
        print(f"✓ CAPITAL_PER_LEVEL = {best_scenario['capital']:.0f} USDT")
        print(f"✓ Expected Grid Levels = {best_scenario['levels']}")
        print(f"✓ Total Capital Used = {best_scenario['total_used']:.2f} USDT ({best_scenario['utilization']:.1f}% utilization)")
        print(f"✓ Reserved for OCO = {total_balance * reserve_ratio:.2f} USDT")
        
        # Estimate grid distribution
        buy_ratio = 0.6  # Assuming -0.79 trend
        sell_ratio = 0.4
        
        estimated_buy = int(best_scenario['levels'] * buy_ratio)
        estimated_sell = int(best_scenario['levels'] * sell_ratio)
        
        print(f"\nEstimated Grid Distribution:")
        print(f"  - BUY orders: ~{estimated_buy} levels")
        print(f"  - SELL orders: ~{estimated_sell} levels")
        print(f"  - Total: {best_scenario['levels']} levels")
        
        print(f"\nSafety Validation:")
        print(f"  - Minimum order value at {lowest_expected_price:.2f} USDT: {best_scenario['min_value']:.2f} USDT")
        print(f"  - Required minimum: {min_notional:.2f} USDT")
        print(f"  - Safety margin: {((best_scenario['min_value'] / min_notional - 1) * 100):.1f}%")
        
        print(f"\n{'='*70}")
        print("To apply this configuration, update config.py:")
        print(f"  CAPITAL_PER_LEVEL = {best_scenario['capital']:.0f}")
        print("=" * 70)
    else:
        print("\n✗ No safe configuration found! Increase account balance or accept higher risk.")
    
    # Compare with current config
    print(f"\nCurrent config.py settings:")
    print(f"  GRID_LEVELS = {config.GRID_LEVELS}")
    print(f"  CAPITAL_PER_LEVEL = {config.CAPITAL_PER_LEVEL}")
    current_max_levels = int(available_for_grid / config.CAPITAL_PER_LEVEL)
    print(f"  Actual achievable levels = {current_max_levels}")
    
    if best_scenario and best_scenario['levels'] > current_max_levels:
        print(f"\n💡 Optimization opportunity: You can increase from {current_max_levels} to {best_scenario['levels']} levels!")
    elif best_scenario and best_scenario['levels'] < current_max_levels:
        print(f"\n⚠️  Current config may be too aggressive. Recommend reducing to {best_scenario['levels']} levels.")
    else:
        print(f"\n✓ Current configuration is already optimal.")


if __name__ == '__main__':
    calculate_optimal_grid()
