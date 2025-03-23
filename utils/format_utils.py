import math

def format_price(price, precision):
    """Format price with appropriate precision ensuring non-zero values
    
    Args:
        price (float): Original price value
        precision (int): Decimal precision to use
    
    Returns:
        str: Formatted price string that is never "0"
    """
    # Validate the price is positive
    if price <= 0:
        return "0.00000001"  # Minimum valid price for Binance
    
    # For small prices, ensure enough decimal places regardless of input precision
    if price < 0.001 and precision < 8:
        # Use 8 decimal places for very small values
        actual_precision = 8
    else:
        # Ensure precision is at least 1 to avoid "0" result
        actual_precision = max(precision, 1)
    
    # Format with appropriate precision
    formatted = "{:.{prec}f}".format(float(price), prec=actual_precision)
    
    # Remove trailing zeros but ensure not returning "0"
    result = formatted.rstrip('0').rstrip('.') if '.' in formatted else formatted
    if result == "0":
        return "0.00000001"  # Return minimum valid price if result is zero
    
    return result

def format_quantity(quantity, precision):
    """Format quantity with appropriate precision
    
    Args:
        quantity (float): Original quantity value
        precision (int): Decimal precision to use
    
    Returns:
        str: Formatted quantity string
    """
    # Ensure quantity is positive
    quantity = abs(float(quantity))
    formatted = math.floor(quantity * 10**precision) / 10**precision
    return '{:.{prec}f}'.format(formatted, prec=precision)

def get_precision_from_filters(filters, filter_type, field_name):
    """Get precision from trading pair filters
    
    Args:
        filters (list): List of filters from symbol info
        filter_type (str): Filter type name (e.g., 'PRICE_FILTER')
        field_name (str): Field name containing step value (e.g., 'tickSize')
    
    Returns:
        int: Precision value
    """
    for f in filters:
        if f['filterType'] == filter_type:
            step_size = f[field_name]
            # Calculate precision from step size
            return get_precision_from_step_size(step_size)
    
    # Default precision if filter not found
    return 8

def get_precision_from_step_size(step_size):
    """Extract precision value from step size
    
    Args:
        step_size (str): Step size value (e.g. "0.00010000" or "1e-5")
    
    Returns:
        int: Number of decimal places for precision
    """
    try:
        # Convert to float to handle scientific notation
        step = float(step_size)
        if step == 0:
            return 0
        
        # Handle different formats
        step_str = str(step)
        if 'e' in step_str.lower():
            # Handle scientific notation (e.g., 1e-8)
            mantissa, exponent = step_str.lower().split('e')
            return abs(int(exponent))
        elif '.' in step_str:
            # Handle decimal notation
            decimal_part = step_str.split('.')[1].rstrip('0')
            return len(decimal_part)
        else:
            # Integer values
            return 0
    except Exception:
        # Return safe default
        return 8