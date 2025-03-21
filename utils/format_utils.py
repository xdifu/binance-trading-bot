import math

def format_price(price, precision):
    """按精度格式化价格
    
    Args:
        price (float): 原始价格
        precision (int): 小数位数精度
    
    Returns:
        str: 格式化后的价格字符串
    """
    formatted = round(float(price), precision)
    return '{:.{prec}f}'.format(formatted, prec=precision)

def format_quantity(quantity, precision):
    """按精度格式化数量
    
    Args:
        quantity (float): 原始数量
        precision (int): 小数位数精度
    
    Returns:
        str: 格式化后的数量字符串
    """
    # 确保数量为正数
    quantity = abs(float(quantity))
    formatted = math.floor(quantity * 10**precision) / 10**precision
    return '{:.{prec}f}'.format(formatted, prec=precision)

def get_precision_from_filters(filters, filter_type, price_filter):
    """从交易对过滤器中获取价格或数量精度
    
    Args:
        filters (list): 交易对过滤器列表
        filter_type (str): 过滤器类型名称
        price_filter (str): 价格或数量步长的键名
    
    Returns:
        int: 精度小数位数
    """
    for f in filters:
        if f['filterType'] == filter_type:
            step_size = f[price_filter]
            # 计算精度
            step_size = float(step_size)
            precision = 0
            if '.' in str(step_size):
                precision = len(str(step_size).split('.')[1])
            return precision
    return 8  # 默认精度
