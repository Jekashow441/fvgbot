import pandas as pd
import numpy as np
from typing import Dict, Optional
from core.settings import cfg
from trading.logger import log_info

def _calc_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df['close'].ewm(span=period, adjust=False).mean()

def _calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def _calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(period).mean()

def _check_displacement(candle: pd.Series) -> bool:
    """Check if the candle has strong momentum (body > required %)."""
    body = abs(candle['close'] - candle['open'])
    wick_to_wick = candle['high'] - candle['low']
    if wick_to_wick == 0:
        return False
    body_pct = (body / wick_to_wick) * 100
    return body_pct >= cfg.fvg_min_body_pct

def detect_fvg(df: pd.DataFrame) -> Optional[Dict]:
    """Detect unmitigated FVGs with ICT rules."""
    if len(df) < 3 + cfg.fvg_confirmations + 20: # +20 for SMA volume calc
        return None
        
    df = df.copy()
    # Calculate SMA volume for displacement check
    df['vol_sma'] = df['volume'].rolling(20).mean()
        
    # Search backwards for a valid FVG
    # Limit search to lookback window
    search_limit = max(20, len(df) - cfg.fvg_lookback)
    
    for i in range(len(df) - 2 - cfg.fvg_confirmations, search_limit, -1):
        c1 = df.iloc[i - 2]
        c2 = df.iloc[i - 1]
        c3 = df.iloc[i]
        
        # Check Displacement and Volume
        if not _check_displacement(c2):
            continue
            
        if cfg.fvg_require_volume and c2['volume'] <= c2['vol_sma']:
            continue
            
        fvg = None
        
        # Bullish FVG
        if c3['low'] > c1['high']:
            gap_size = (c3['low'] - c1['high']) / c1['high'] * 100
            if gap_size >= cfg.fvg_min_size_pct:
                fvg = {
                    "type": "BULLISH",
                    "top": float(c3['low']),
                    "bottom": float(c1['high']),
                    "candle_time": int(c2.name if 'timestamp' not in c2 else c2['timestamp']),
                    "gap_size": float(gap_size),
                    "index": int(i),
                    "ce": float(c1['high'] + (c3['low'] - c1['high']) / 2) # Consequent Encroachment (50%)
                }
                
        # Bearish FVG
        elif c3['high'] < c1['low']:
            gap_size = (c1['low'] - c3['high']) / c1['low'] * 100
            if gap_size >= cfg.fvg_min_size_pct:
                fvg = {
                    "type": "BEARISH",
                    "top": float(c1['low']),
                    "bottom": float(c3['high']),
                    "candle_time": int(c2.name if 'timestamp' not in c2 else c2['timestamp']),
                    "gap_size": float(gap_size),
                    "index": int(i),
                    "ce": float(c3['high'] + (c1['low'] - c3['high']) / 2) # Consequent Encroachment (50%)
                }
                
        if fvg:
            # Mitigation Check: Verify if FVG was invalidated by ANY candle from c3 to current
            is_valid = True
            for j in range(i + 1, len(df)):
                check_c = df.iloc[j]
                
                if fvg['type'] == 'BULLISH':
                    # Strict CE mitigation (ICT): Close below 50% invalidates
                    if cfg.fvg_strict_mitigation and check_c['close'] < fvg['ce']:
                        is_valid = False
                        break
                    # Full mitigation: low sweeps below bottom
                    if check_c['low'] <= fvg['bottom']:
                        is_valid = False
                        break
                else: # BEARISH
                    # Strict CE mitigation (ICT): Close above 50% invalidates
                    if cfg.fvg_strict_mitigation and check_c['close'] > fvg['ce']:
                        is_valid = False
                        break
                    # Full mitigation: high sweeps above top
                    if check_c['high'] >= fvg['top']:
                        is_valid = False
                        break
                        
            if is_valid:
                return fvg
                
    return None

def validate_signal(df: pd.DataFrame, fvg: Optional[Dict]) -> Optional[Dict]:
    """Validate if current price is tapping into the FVG to generate a signal."""
    if not fvg:
        return None
        
    df = df.copy()
    df['ema_fast'] = _calc_ema(df, 50)
    df['ema_slow'] = _calc_ema(df, cfg.ema_period)
    df['rsi'] = _calc_rsi(df)
    df['atr'] = _calc_atr(df)
    
    last_candle = df.iloc[-1]
    last_close = last_candle['close']
    ema_fast = last_candle['ema_fast']
    ema_slow = last_candle['ema_slow']
    rsi = last_candle['rsi']
    atr = last_candle['atr']
    
    # 1. EMA Trend Filter
    if fvg['type'] == 'BULLISH' and (last_close < ema_slow or ema_fast < ema_slow):
        return None
    if fvg['type'] == 'BEARISH' and (last_close > ema_slow or ema_fast > ema_slow):
        return None
        
    # 2. RSI Filter
    if fvg['type'] == 'BULLISH' and rsi > cfg.rsi_overbought:
        return None
    if fvg['type'] == 'BEARISH' and rsi < cfg.rsi_oversold:
        return None
        
    # 3. Tap Check (Signal generation)
    if fvg['type'] == 'BULLISH':
        # Price dropped into the FVG (tap)
        if last_candle['low'] <= fvg['top'] and last_candle['close'] > fvg['bottom']:
            
            # Additional check: Did it close below CE? If so, we don't enter (ICT rule)
            if cfg.fvg_strict_mitigation and last_candle['close'] < fvg['ce']:
                return None
                
            # Structural SL with ATR buffer
            sl = float(fvg['bottom'])
            if cfg.struct_sl:
                swing_low = df['low'].tail(cfg.swing_lookback).min()
                sl = float(min(fvg['bottom'], swing_low) - (atr * cfg.sl_atr_buffer))
                
            risk = last_close - sl
            rr = cfg.risk_reward
            if cfg.dynamic_rr:
                rr = min(cfg.rr_max, max(cfg.rr_min, rr * (atr / last_close * 100))) 
                
            tp = float(last_close + (risk * rr))
            return {"signal": "LONG", "entry": float(last_close), "sl": sl, "tp": tp, "rr": rr}
            
    else: # BEARISH
        # Price rallied into the FVG (tap)
        if last_candle['high'] >= fvg['bottom'] and last_candle['close'] < fvg['top']:
            
            # Additional check: Did it close above CE? If so, we don't enter
            if cfg.fvg_strict_mitigation and last_candle['close'] > fvg['ce']:
                return None
                
            sl = float(fvg['top'])
            if cfg.struct_sl:
                swing_high = df['high'].tail(cfg.swing_lookback).max()
                sl = float(max(fvg['top'], swing_high) + (atr * cfg.sl_atr_buffer))
                
            risk = sl - last_close
            rr = cfg.risk_reward
            if cfg.dynamic_rr:
                rr = min(cfg.rr_max, max(cfg.rr_min, rr * (atr / last_close * 100)))
                
            tp = float(last_close - (risk * rr))
            return {"signal": "SHORT", "entry": float(last_close), "sl": sl, "tp": tp, "rr": rr}
            
    return None
