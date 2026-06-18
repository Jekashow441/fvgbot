import io
import pandas as pd
import mplfinance as mpf
import matplotlib

# Set non-interactive backend to avoid GUI errors
matplotlib.use('Agg')

def generate_fvg_chart(symbol: str, df: pd.DataFrame, fvg: dict, signal: dict) -> io.BytesIO:
    """
    Generates a chart with candlestick data, the FVG zone, and the signal levels.
    Returns an io.BytesIO buffer containing the PNG image.
    """
    # Create a copy and slice the last 60 candles for a clean view
    plot_df = df.tail(60).copy()
    
    # mplfinance requires a DatetimeIndex
    plot_df['datetime'] = pd.to_datetime(plot_df['timestamp'], unit='ms')
    plot_df.set_index('datetime', inplace=True)
    
    # Calculate the FVG zone colors
    is_bullish = fvg['type'] == 'BULLISH'
    fvg_color = 'rgba(0, 255, 0, 0.2)' if is_bullish else 'rgba(255, 0, 0, 0.2)' # This is just for conceptualizing, we use axhspan
    
    # Setup the plot
    mc = mpf.make_marketcolors(up='#16c784', down='#ea3943', inherit=True)
    s  = mpf.make_mpf_style(
        marketcolors=mc, 
        gridstyle=':', 
        y_on_right=True, 
        facecolor='#121620', 
        edgecolor='#232a38', 
        figcolor='#0b0e14', 
        rc={
            'text.color': '#e6e9ef',
            'axes.labelcolor': '#e6e9ef',
            'xtick.color': '#e6e9ef',
            'ytick.color': '#e6e9ef',
        }
    )
    
    fig, axlist = mpf.plot(
        plot_df,
        type='candle',
        style=s,
        title=f"\n{symbol} - {fvg['type']} FVG Setup",
        ylabel='Price',
        returnfig=True,
        figsize=(10, 6)
    )
    
    ax = axlist[0]
    
    # 1. Draw the FVG Zone
    zone_color = '#16c784' if is_bullish else '#ea3943'
    ax.axhspan(fvg['bottom'], fvg['top'], alpha=0.2, color=zone_color, label='FVG Zone')
    
    # 2. Draw Entry, SL, TP lines from the signal candle if signal exists
    last_idx = len(plot_df) - 1
    
    if signal:
        # Entry line (Yellow)
        ax.hlines(signal['entry'], xmin=last_idx-5, xmax=last_idx+2, colors='#f0a020', linestyles='solid', linewidth=2)
        ax.text(last_idx-5, signal['entry'], ' ENTRY', color='#f0a020', va='bottom', fontsize=10, fontweight='bold')
        
        # TP line (Green)
        ax.hlines(signal['tp'], xmin=last_idx-5, xmax=last_idx+2, colors='#16c784', linestyles='dashed', linewidth=2)
        ax.text(last_idx-5, signal['tp'], ' TP', color='#16c784', va='bottom', fontsize=10, fontweight='bold')
        
        # SL line (Red)
        ax.hlines(signal['sl'], xmin=last_idx-5, xmax=last_idx+2, colors='#ea3943', linestyles='dashed', linewidth=2)
        ax.text(last_idx-5, signal['sl'], ' SL', color='#ea3943', va='bottom', fontsize=10, fontweight='bold')
        
        # Draw arrow from Entry to TP
        ax.annotate(
            '',
            xy=(last_idx + 1, signal['tp']),
            xytext=(last_idx + 1, signal['entry']),
            arrowprops=dict(arrowstyle='->', color=zone_color, lw=2)
        )
    
    # Save to buffer
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=120)
    buf.seek(0)
    
    # Close figure to free memory
    import matplotlib.pyplot as plt
    plt.close(fig)
    
    return buf
