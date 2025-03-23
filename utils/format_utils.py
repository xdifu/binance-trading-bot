import math
import decimal

def format_price(price, precision):
    """Format price with appropriate precision ensuring non-zero values and adherence to exchange tick size
    
    Args:
        price (float): Original price value
        precision (int): Decimal precision to use (from exchange info)
    
    Returns:
        str: Formatted price string that matches exchange tick size requirements
    """
    # Validate the price is positive
    if price <= 0:
        return "0.00000001"  # Minimum valid price for Binance
    
    # Calculate tick size based on precision (e.g. precision 7 -> tick size 0.0000001)
    tick_size = decimal.Decimal('0.1') ** precision
    
    # Convert to decimal for more precise arithmetic with small values
    decimal_price = decimal.Decimal(str(price))
    
    # Round to nearest valid tick size
    rounded_price = round(decimal_price / tick_size) * tick_size
    
    # Format to required decimal places - ensure we don't lose precision
    formatted = f"{rounded_price:.{precision}f}"
    
    # Remove trailing zeros but ensure we're never returning "0"
    result = formatted.rstrip('0').rstrip('.') if '.' in formatted else formatted
    if result == "0":
        return "0." + "0" * (precision - 1) + "1"  # Return smallest valid price based on precision
    
    return result

def format_quantity(quantity, precision):
    """Format quantity with appropriate precision - ensuring compliance with LOT_SIZE filter
    
    Args:
        quantity (float): Original quantity value
        precision (int): Decimal precision to use (from exchange info)
    
    Returns:
        str: Formatted quantity string
    """
    # Ensure quantity is positive
    quantity = abs(float(quantity))
    
    # Calculate step size based on precision (e.g. precision 0 -> step 1, precision 2 -> step 0.01)
    step_size = 10 ** -precision
    
    # Floor to nearest valid step size (Binance requires this for quantity)
    floored_quantity = math.floor(quantity / step_size) * step_size
    
    # Format to correct number of decimal places
    return f"{floored_quantity:.{precision}f}"

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
    return 8 if filter_type == 'PRICE_FILTER' else 0

def get_precision_from_step_size(step_size):
    """Extract precision value from step size
    
    Args:
        step_size (str): Step size value (e.g. "0.00010000" or "1e-5")
    
    Returns:
        int: Number of decimal places for precision
    """
    try:
        # Convert to decimal to handle scientific notation properly
        step = decimal.Decimal(str(step_size))
        if step == 0:
            return 0
        
        # Handle different formats
        normalized = step.normalize()
        if normalized == normalized.to_integral():
            # Integer values
            return 0
        
        # Get precision from normalized decimal - convert to string to count decimal places
        step_str = str(normalized)
        if 'E' in step_str or 'e' in step_str:
            # Handle scientific notation
            parts = step_str.lower().split('e')
            if len(parts) == 2:
                if parts[1].startswith('-'):
                    return abs(int(parts[1]))
                else:
                    mantissa = parts[0].replace('.', '')
                    exponent = int(parts[1])
                    if exponent < len(mantissa):
                        return 0
                    return exponent - len(mantissa) + 1
        elif '.' in step_str:
            # Handle decimal notation
            decimal_part = step_str.split('.')[1].rstrip('0')
            return len(decimal_part)
        
        return 0
    except Exception:
        # Return safe default if something goes wrong
        return 8