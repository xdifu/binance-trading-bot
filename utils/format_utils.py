import math
import decimal
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, getcontext

# 设置更高的精度以处理各种货币 (包括高值如BTC和低值如SHIB)
getcontext().prec = 28

def format_price(price, precision):
    """Format price with appropriate precision ensuring non-zero values and adherence to exchange tick size
    
    Args:
        price (float|str|Decimal): Original price value
        precision (int): Decimal precision to use (from exchange info)
    
    Returns:
        str: Formatted price string that matches exchange tick size requirements
    """
    # 验证输入参数
    if precision < 0:
        precision = 0  # 保证精度非负
    
    try:
        # 转换为Decimal类型以确保精确计算
        decimal_price = Decimal(str(price)) if not isinstance(price, Decimal) else price
    except (decimal.InvalidOperation, TypeError):
        # 处理无效输入
        return f"0.{'0' * (precision - 1)}1" if precision > 0 else "1"
        
    # 验证价格是正数
    if decimal_price <= Decimal('0'):
        # 返回基于精度的最小有效价格
        return f"0.{'0' * (precision - 1)}1" if precision > 0 else "1"
    
    # 计算基于精度的tick_size (例如 精度为7 -> tick size 0.0000001)
    tick_size = Decimal('0.1') ** precision
    
    # 四舍五入至最接近的tick_size
    remainder = decimal_price % tick_size
    
    # 如果余数非常接近tick_size (由于浮点精度的限制)，向上取整
    if remainder > Decimal('0') and (tick_size - remainder) / tick_size < Decimal('0.000001'):
        # 几乎是tick_size的整数倍，直接进位
        rounded_price = decimal_price - remainder + tick_size
    else:
        # 标准四舍五入到最近的tick_size
        rounded_price = decimal_price - remainder
        # 如果余数大于等于tick_size的一半，进位
        if remainder >= (tick_size / Decimal('2')):
            rounded_price += tick_size
    
    # 确保四舍五入后的价格是tick_size的整数倍
    # 这是交易所要求的，避免订单被拒绝
    if rounded_price % tick_size != Decimal('0'):
        # 通过除后再乘处理任何可能的精度偏差
        factor = rounded_price / tick_size
        whole_factor = Decimal(int(factor))  # 取整数部分
        rounded_price = whole_factor * tick_size
    
    # 格式化为所需小数位数，确保不损失精度
    formatted = f"{rounded_price:.{precision}f}"
    
    # 移除尾随零但确保不返回"0"
    result = formatted.rstrip('0').rstrip('.') if '.' in formatted else formatted
    if result == "0":
        # 返回基于精度的最小有效价格
        return f"0.{'0' * (precision - 1)}1" if precision > 0 else "1"
    
    return result

def format_quantity(quantity, precision):
    """Format quantity with appropriate precision - ensuring compliance with LOT_SIZE filter
    
    Args:
        quantity (float|str|Decimal): Original quantity value
        precision (int): Decimal precision to use (from exchange info)
    
    Returns:
        str: Formatted quantity string
    """
    # 验证输入参数
    if precision < 0:
        precision = 0  # 保证精度非负
        
    try:
        # 转换为Decimal类型以确保精确计算
        decimal_quantity = Decimal(str(quantity)) if not isinstance(quantity, Decimal) else quantity
    except (decimal.InvalidOperation, TypeError):
        # 处理无效输入
        return f"0.{'0' * precision}" if precision > 0 else "0"
    
    # 确保数量为正数
    decimal_quantity = abs(decimal_quantity)
    
    if decimal_quantity == Decimal('0'):
        # 返回零，但确保格式正确
        return f"0.{'0' * precision}" if precision > 0 else "0"
    
    # 计算基于精度的step_size (例如 精度为0 -> step 1, 精度为2 -> step 0.01)
    step_size = Decimal('0.1') ** precision
    
    # Binance要求数量必须向下取整到step_size的倍数
    # 使用ROUND_FLOOR确保总是向下取整
    factor = decimal_quantity / step_size
    whole_factor = Decimal(math.floor(factor))  # 向下取整
    floored_quantity = whole_factor * step_size
    
    # 格式化为正确的小数位数
    formatted = f"{floored_quantity:.{precision}f}"
    
    # 确保不会因为数值太小而格式化为"0"
    if formatted == f"0.{'0' * precision}":
        # 如果向下取整后为零但原数量不为零，返回最小有效数量
        if decimal_quantity > Decimal('0'):
            return f"0.{'0' * (precision - 1)}1" if precision > 0 else "1"
    
    return formatted

def get_precision_from_filters(filters, filter_type, field_name):
    """Get precision from trading pair filters
    
    Args:
        filters (list): List of filters from symbol info
        filter_type (str): Filter type name (e.g., 'PRICE_FILTER')
        field_name (str): Field name containing step value (e.g., 'tickSize')
    
    Returns:
        int: Precision value
    """
    if not filters or not isinstance(filters, list):
        return 8 if filter_type == 'PRICE_FILTER' else 0
        
    for f in filters:
        if isinstance(f, dict) and f.get('filterType') == filter_type:
            step_size = f.get(field_name)
            if step_size:
                # 计算步长的精度
                return get_precision_from_step_size(step_size)
    
    # 默认精度（如果找不到过滤器）
    return 8 if filter_type == 'PRICE_FILTER' else 0

def get_precision_from_step_size(step_size):
    """Extract precision value from step size
    
    Args:
        step_size (str): Step size value (e.g. "0.00010000" or "1e-5")
    
    Returns:
        int: Number of decimal places for precision
    """
    try:
        # 转换为Decimal以正确处理科学记数法
        step = Decimal(str(step_size))
        if step == 0:
            return 0
        
        # 处理规范化的形式
        normalized = step.normalize()
        if normalized == normalized.to_integral():
            # 整数值
            return 0
        
        # 从规范化的小数中获取精度 - 转换为字符串以统计小数位
        step_str = str(normalized)
        if 'E' in step_str or 'e' in step_str:
            # 处理科学记数法
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
            # 处理小数表示法
            decimal_part = step_str.split('.')[1].rstrip('0')
            return len(decimal_part)
        
        return 0
    except Exception:
        # 如果出现错误则返回安全的默认值
        return 8 if str(step_size).startswith('0.') else 0

def validate_price_quantity(price, quantity, symbol_info):
    """验证价格和数量是否符合交易对的交易规则
    
    Args:
        price (float|str): 价格
        quantity (float|str): 数量
        symbol_info (dict): 交易对信息
        
    Returns:
        tuple: (bool, str) - (是否有效, 错误消息)
    """
    if not symbol_info or 'filters' not in symbol_info:
        return True, ""  # 无法验证，假设有效
        
    try:
        decimal_price = Decimal(str(price))
        decimal_quantity = Decimal(str(quantity))
        
        # 价格验证
        for f in symbol_info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                min_price = Decimal(f['minPrice'])
                max_price = Decimal(f['maxPrice'])
                tick_size = Decimal(f['tickSize'])
                
                if decimal_price < min_price:
                    return False, f"Price {price} below minimum {min_price}"
                if decimal_price > max_price:
                    return False, f"Price {price} above maximum {max_price}"
                
                # 验证价格是否是tick_size的倍数
                remainder = decimal_price % tick_size
                if remainder != Decimal('0') and (tick_size - remainder) / tick_size > Decimal('0.000001'):
                    return False, f"Price {price} not a multiple of tick size {tick_size}"
                    
            # 数量验证
            elif f['filterType'] == 'LOT_SIZE':
                min_qty = Decimal(f['minQty'])
                max_qty = Decimal(f['maxQty'])
                step_size = Decimal(f['stepSize'])
                
                if decimal_quantity < min_qty:
                    return False, f"Quantity {quantity} below minimum {min_qty}"
                if decimal_quantity > max_qty:
                    return False, f"Quantity {quantity} above maximum {max_qty}"
                
                # 验证数量是否是step_size的倍数
                remainder = decimal_quantity % step_size
                if remainder != Decimal('0') and remainder / step_size > Decimal('0.000001'):
                    return False, f"Quantity {quantity} not a multiple of step size {step_size}"
                    
            # 市值验证
            elif f['filterType'] == 'NOTIONAL':
                min_notional = Decimal(f['minNotional'])
                notional = decimal_price * decimal_quantity
                
                if notional < min_notional:
                    return False, f"Order value {notional} below minimum {min_notional}"
        
        return True, ""
    except Exception as e:
        return False, f"Validation error: {str(e)}"