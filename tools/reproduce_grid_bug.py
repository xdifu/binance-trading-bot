
def calculate_optimal_grid_center_simulation():
    # Mock data
    # Create 400 candles (limit=400)
    # Price constant at 100 for simplicity
    historical_klines = [[0, 0, 0, 0, 100.0, 0] for _ in range(400)]
    
    print(f"Total klines: {len(historical_klines)}")
    
    # 3. Segment data into time periods (COPIED LOGIC)
    recent_data = historical_klines[-42:]     # Last 7 days (most important)
    medium_data = historical_klines[-126:-42] # 7-21 days ago
    long_data = historical_klines[:-126]      # 21-60 days ago
    
    print(f"Recent data len: {len(recent_data)}")
    print(f"Medium data len: {len(medium_data)}")
    print(f"Long data len: {len(long_data)}")
    
    # 4. Calculate time-weighted average prices (COPIED LOGIC)
    def calculate_weighted_avg(klines):
        total_weight = 0
        weighted_sum = 0
        
        # Weight more recent candles higher within each segment
        for i, k in enumerate(klines):
            # if isinstance(k, list) and len(k) > 4: # Simplified for mock
            # Simple linear weight - newer data gets higher weight
            weight = (i + 1) / len(klines)
            close_price = float(k[4])  # Close price
            weighted_sum += close_price * weight
            total_weight += weight
        
        return weighted_sum / total_weight if total_weight > 0 else 0
        
    recent_avg = calculate_weighted_avg(recent_data)
    medium_avg = calculate_weighted_avg(medium_data)
    long_avg = calculate_weighted_avg(long_data)
    
    print(f"Recent avg: {recent_avg}")
    print(f"Medium avg: {medium_avg}")
    print(f"Long avg: {long_avg}")
    
    # 5. Calculate oscillation degree (Mock)
    trend_strength = 0.1
    oscillation_index = 1 - abs(trend_strength)
    
    # 6. Dynamic time weights (COPIED LOGIC)
    recent_weight = 0.45 + (oscillation_index * 0.1)  # 0.45-0.55 range
    medium_weight = 0.30
    long_weight = 0.25 - (oscillation_index * 0.1)    # 0.15-0.25 range
    
    print(f"Weights: Recent={recent_weight}, Medium={medium_weight}, Long={long_weight}")
    
    # 7. Calculate historical weighted center (COPIED LOGIC)
    historical_center = (
        recent_avg * recent_weight + 
        medium_avg * medium_weight + 
        long_avg * long_weight
    )
    
    print(f"Historical center: {historical_center}")
    
    current_price = 100.0
    
    # 9. Calculate price deviation
    price_deviation = abs(current_price - historical_center) / historical_center
    print(f"Price deviation: {price_deviation}")

if __name__ == "__main__":
    calculate_optimal_grid_center_simulation()
