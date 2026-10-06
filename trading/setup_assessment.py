"""Causal diagnostic score before all entry gates; never creates a signal."""
from trading.strategy import market_context, _score_signal, validate_signal
from trading.context import structure_context, trendline_context


def assess_zones(df,zones,htf,market,settings):
    ctx=market_context(df,settings)
    structure=structure_context(df,settings.structure_pivot)
    atr=(ctx.get('atr_pct') or 0)*float(df.iloc[-1].close)/100
    lines=trendline_context(df,settings.structure_pivot,atr,settings.trendline_tolerance_atr)
    result={}
    for zone in zones:
        side='LONG' if zone['type']=='BULLISH' else 'SHORT'
        expected='UP' if side=='LONG' else 'DOWN'
        score,factors=_score_signal(side,zone,ctx,htf,ctx.get('rsi'),ctx.get('adx'),settings)
        if lines['bullish_retest' if side=='LONG' else 'bearish_retest']:
            score=min(100,score+8)
        diagnostics={}
        candidate=validate_signal(df,zone,htf,market,settings=settings,diagnostics=diagnostics)
        # These limits cannot recover on a later candle for the same zone.
        # Record them even when validation exits earlier on a missing retest.
        exhausted=zone.get('touches',0)>2 or zone.get('touch_bars',0)>settings.fvg_max_touch_bars
        if zone.get('touches',0)>2:
            diagnostics['zone_exhausted']=1
        if zone.get('touch_bars',0)>settings.fvg_max_touch_bars:
            diagnostics['prolonged_zone_residence']=1
        price=float(df.iloc[-1].close)
        distance=max(zone['bottom']-price,price-zone['top'],0)
        htf_aligned=bool(htf and htf.get('trend')==expected)
        structure_aligned=structure['direction']==expected
        sweep=structure['sweep']==('SELL_SIDE' if side=='LONG' else 'BUY_SIDE')
        checks={'FVG':True,'Zone eligibility':not exhausted,'HTF bias':htf_aligned,'Structure':structure_aligned,'Liquidity':sweep,'Entry':candidate is not None}
        may_form=htf_aligned and structure_aligned and sweep and candidate is None and set(diagnostics)<= {'no_recent_retest','no_bullish_rejection','no_bearish_rejection','entry_too_far'}
        stage='WATCHLIST'
        if htf_aligned and not exhausted:
            stage='DEVELOPING'
            if structure['direction'] in ('RANGE',expected):
                stage='POTENTIAL'
                if atr>0 and distance<=.25*atr:
                    stage='ENTRY APPROACHING'
        result[str(zone['candle_time'])+zone['type']]={'score':score,'score_kind':'pre-learning rule score','candidate':candidate,
            'status':stage,'signal_may_form':may_form,'checks':checks,'structure':structure,'current_price':price,
            'rejections':list(diagnostics),'analysis_bar':int(df.iloc[-1].timestamp),
            'required_next_conditions':['Retest after FVG formation within the configured closed-bar window.',
                'Directional closed candle beyond CE, within the entry distance and volume limits.',
                'All HTF/structure/price/cost/news/book/risk gates must pass; score >= '+str(settings.min_signal_score)+'.'],
            'policy':{'min_score':settings.min_signal_score,'min_turnover':settings.min_turnover_24h}}
    return result
