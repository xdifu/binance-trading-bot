import pandas as pd
import numpy as np

def calculate_atr(klines, period=14):
    """计算ATR指标"""
    df = pd.DataFrame(klines, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 
                                      'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                                      'taker_buy_quote', 'ignored'])
    
    # 转换为float
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col].astype(float)
    
    # 计算TR (True Range)
    df['prev_close'] = df['close'].shift(1)
    df['tr1'] = abs(df['high'] - df['low'])
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    
    # 计算ATR
    df['atr'] = df['tr'].rolling(window=period).mean()
    
    return df['atr'].iloc[-1] if not df['atr'].empty else None