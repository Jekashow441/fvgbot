"""Trader-style commentary over observable data. Describes, never predicts or promises."""
import time
from collections import deque

FEED = deque(maxlen=300)
TREND_RU = {"UP": "восходящий", "DOWN": "нисходящий", "RANGE": "боковик", "UNKNOWN": "неясен"}
STAGE_RANK = {"ENTRY APPROACHING": 3, "POTENTIAL": 2, "DEVELOPING": 1, "WATCHLIST": 0}
WAIT_RU = {
    "no_recent_retest": "ретест зоны", "no_bullish_rejection": "бычью свечу отбоя от зоны",
    "no_bearish_rejection": "медвежью свечу отбоя от зоны", "entry_too_far": "возврат цены ближе к зоне",
    "htf_opposition": "разворот старшего ТФ", "higher_4h_opposition": "разворот 4h", "thin_orderbook": "более глубокий стакан",
    "extended_from_vwap": "откат к VWAP", "structure_opposition": "слом структуры в нашу сторону",
    "low_relative_volume": "подтверждение объёмом", "ema_opposition": "возврат цены за EMA",
    "score_below_threshold": "больше подтверждений (score)", "critical_negative_news": "прояснения ситуации с новостями",
    "rsi_extreme": "охлаждения RSI", "stop_too_wide": "более компактную структуру для стопа",
}


def _f(value, digits=2):
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def record(kind, symbol, text, level="info", now=None):
    FEED.appendleft({"t": time.time() if now is None else now, "kind": kind, "symbol": symbol, "text": text, "level": level})


def feed(limit=60):
    return sorted(FEED, key=lambda e: -e["t"])[:limit]


def _best_assessment(info):
    zones = {str(z.get("candle_time")) + z.get("type", ""): z for z in info.get("zones") or []}
    best = None
    for key, a in (info.get("assessments") or {}).items():
        zone = zones.get(key)
        if not zone or (a.get("checks") or {}).get("Zone eligibility") is False:
            continue
        rank = (STAGE_RANK.get(a.get("status"), -1), a.get("score") or 0)
        if best is None or rank > best[0]:
            best = (rank, zone, a)
    return best and best[1:]


def commentary(symbol, info, news=None, active=None):
    """A short read of the coin the way a discretionary trader would narrate their screen."""
    info = info or {}
    intel = info.get("intelligence") or {}
    htf = info.get("htf_context") or {}
    market = info.get("market") or {}
    coin_news = (news or info.get("news") or {}).get("coin") or {}
    lines, bias = [], "NEUTRAL"

    trend, adx = intel.get("trend"), intel.get("adx")
    strength = "" if adx is None else " — тренд сильный" if adx >= 30 else " — тренд слабый" if adx < 20 else ""
    if trend:
        line = f"Младший ТФ: {TREND_RU.get(trend, trend)} (ADX {_f(adx, 0)}{strength})"
        if htf.get("trend"):
            agree = "совпадает" if htf["trend"] == trend else "не совпадает"
            line += f", старший ТФ: {TREND_RU.get(htf['trend'], htf['trend'])} — {agree}"
        lines.append(line + ".")
    higher = intel.get("higher_4h") or {}
    if higher.get("trend") and higher["trend"] != "UNKNOWN":
        lines.append(f"На 4h {TREND_RU.get(higher['trend'], higher['trend'])} тренд.")

    regime = intel.get("regime")
    if regime == "SHOCK":
        lines.append("Сейчас всплеск волатильности: свечи в 3+ ATR. В такие моменты лучше не торопиться.")
    elif regime == "RANGE":
        lines.append("Рынок по монете во флэте — работают отбои от границ, пробои часто ложные.")

    rvol = intel.get("relative_volume")
    if rvol is not None and rvol >= 2:
        lines.append(f"Объём в {_f(rvol, 1)}× выше среднего — на монете повышенная активность.")
    elif rvol is not None and rvol < 0.5:
        lines.append("Объём заметно ниже среднего — интерес к монете слабый.")

    vwap = intel.get("distance_vwap_atr")
    if vwap is not None and abs(vwap) >= 2.5:
        lines.append(f"Цена растянута от VWAP на {_f(abs(vwap), 1)} ATR {'вверх' if vwap > 0 else 'вниз'} — догонять рискованно, вероятен откат.")

    bench = intel.get("benchmark") or {}
    rel = bench.get("relative_return")
    if rel is not None and abs(rel) >= 0.01:
        lines.append(f"{'Сильнее' if rel > 0 else 'Слабее'} BTC на {_f(abs(rel)*100, 1)}% за последние ~8 часов"
                     + (f", корреляция {_f(bench.get('correlation'))}." if bench.get("correlation") is not None else "."))

    funding = market.get("funding_rate")
    if funding is not None and abs(funding) >= 0.0003:
        crowd = "лонги переплачивают — толпа в лонгах" if funding > 0 else "шорты переплачивают — толпа в шортах"
        lines.append(f"Funding {_f(funding*100, 3)}%: {crowd}, возможен squeeze в обратную сторону.")
    change = market.get("change_24h")
    if change is not None and abs(change) >= 0.08:
        lines.append(f"За сутки {'+' if change > 0 else ''}{_f(change*100, 1)}% — монета в движении.")

    if coin_news.get("critical"):
        lines.append("⚠ Критичная новость: " + "; ".join(coin_news.get("critical_titles") or [])[:220] + ". Лонги не рассматриваю.")
    elif coin_news.get("count"):
        tone = {"BULLISH": "позитивный", "BEARISH": "негативный", "NEUTRAL": "нейтральный"}.get(coin_news.get("label"), "смешанный")
        head = (coin_news.get("headlines") or [{}])[0].get("title", "")
        lines.append(f"Новости: {coin_news.get('count_24h', 0)} за сутки, тон {tone}. Главное: «{head[:160]}».")
    elif coin_news.get("status") == "OK":
        lines.append("Свежих новостей по монете нет — движение, если есть, техническое.")

    signal = info.get("signal")
    best = _best_assessment(info)
    if active:
        t = active
        lines.append(f"Я в позиции {t['side']} от {t['entry']:.6g}: стоп {t['sl']:.6g}, цель {t['tp']:.6g}"
                     + (f", сейчас {_f(t.get('r'))}R." if t.get("r") is not None else "."))
        verdict, bias = "Сопровождаю открытую позицию.", t["side"]
    elif signal and not info.get("blockers"):
        verdict, bias = f"Сигнал {signal['signal']}: вход {signal['entry']:.6g}, стоп {signal['sl']:.6g}, цель {signal['tp']:.6g}.", signal["signal"]
    elif best:
        zone, a = best
        side = "LONG" if zone["type"] == "BULLISH" else "SHORT"
        lines.append(f"Вижу {'бычью' if side == 'LONG' else 'медвежью'} FVG-зону {zone['bottom']:.6g}–{zone['top']:.6g} (score {a.get('score')}, {a.get('status')}).")
        waits = [WAIT_RU[r] for r in (a.get("rejections") or []) + (a.get("execution_rejections") or []) if r in WAIT_RU]
        waits = list(dict.fromkeys(waits))
        verdict = f"Жду {', '.join(waits[:3])} для {side}." if waits else f"Наблюдаю за зоной для {side}."
        bias = side if a.get("status") in ("ENTRY APPROACHING", "POTENTIAL") else "NEUTRAL"
    elif info.get("cooldown_until"):
        verdict = "Пауза после убытка по монете — не торгую до " + str(info["cooldown_until"])[11:16] + "."
    else:
        verdict = "Рабочих FVG-зон нет — монета в списке наблюдения, ничего не делаю."
    return {"symbol": symbol, "lines": lines, "verdict": verdict, "bias": bias}


def market_brief(latest, market, benchmarks, now=None):
    now = time.time() if now is None else now
    eligible = {s: r for s, r in market.items() if r.get("eligible")}
    breadth = next((r.get("breadth") for r in market.values() if r.get("breadth") is not None), None)
    btc, eth = (benchmarks.get("BTCUSDT") or {}), (benchmarks.get("ETHUSDT") or {})
    movers = sorted(((s, r.get("change_24h")) for s, r in eligible.items() if r.get("change_24h") is not None), key=lambda x: x[1])
    funding = sorted(((s, r.get("funding_rate")) for s, r in eligible.items() if r.get("funding_rate") is not None), key=lambda x: x[1])
    hot = sorted(((s, (i.get("intelligence") or {}).get("relative_volume")) for s, i in latest.items()
                  if (i or {}).get("intelligence") and (i["intelligence"].get("relative_volume") or 0) >= 2.5), key=lambda x: -x[1])
    stages = {}
    for info in latest.values():
        for a in ((info or {}).get("assessments") or {}).values():
            stages[a.get("status")] = stages.get(a.get("status"), 0) + 1
    news_bull, news_bear = [], []
    for s, info in latest.items():
        coin = ((info or {}).get("news") or {}).get("coin") or {}
        if coin.get("critical") or (coin.get("sentiment") is not None and coin["sentiment"] <= -0.3):
            news_bear.append({"symbol": s, "sentiment": coin.get("sentiment"), "title": (coin.get("headlines") or [{}])[0].get("title")})
        elif coin.get("sentiment") is not None and coin["sentiment"] >= 0.3:
            news_bull.append({"symbol": s, "sentiment": coin.get("sentiment"), "title": (coin.get("headlines") or [{}])[0].get("title")})

    mood = []
    if btc.get("trend"):
        mood.append(f"BTC: {TREND_RU.get(btc['trend'], btc['trend'])} (ADX {_f(btc.get('adx'), 0)})")
    if eth.get("trend"):
        mood.append(f"ETH: {TREND_RU.get(eth['trend'], eth['trend'])}")
    if breadth is not None:
        tone = "покупатели доминируют" if breadth >= 0.65 else "продавцы доминируют" if breadth <= 0.35 else "силы примерно равны"
        mood.append(f"{round(breadth*100)}% оборота в растущих монетах — {tone}")
    risk = "risk-on" if btc.get("trend") == "UP" and (breadth or 0) >= 0.55 else "risk-off" if btc.get("trend") == "DOWN" and (breadth or 1) <= 0.45 else "смешанный"
    return {
        "as_of": now, "mood": mood, "regime": risk,
        "summary": f"Режим рынка: {risk}. " + "; ".join(mood) + "." if mood else "Данных по рынку пока нет.",
        "gainers": [{"symbol": s, "change": c} for s, c in movers[::-1][:6]],
        "losers": [{"symbol": s, "change": c} for s, c in movers[:6]],
        "funding_high": [{"symbol": s, "funding": f} for s, f in funding[::-1][:5] if f > 0],
        "funding_low": [{"symbol": s, "funding": f} for s, f in funding[:5] if f < 0],
        "volume_spikes": [{"symbol": s, "rvol": v} for s, v in hot[:8]],
        "stages": stages, "news_bullish": news_bull[:6], "news_bearish": news_bear[:6],
    }


def observe(symbol, before, after, now=None):
    """Turn state changes into a live feed, like a trader calling out what they notice."""
    before, after = before or {}, after or {}
    sig_b, sig_a = before.get("signal"), after.get("signal")
    if sig_a and not after.get("blockers") and (not sig_b or sig_b.get("signal_id") != sig_a.get("signal_id")):
        record("signal", symbol, f"Сигнал {sig_a['signal']} (score {sig_a.get('score')}): вход {sig_a['entry']:.6g}, стоп {sig_a['sl']:.6g}, цель {sig_a['tp']:.6g}", "good", now)
    old = {k: a.get("status") for k, a in (before.get("assessments") or {}).items()}
    for key, a in (after.get("assessments") or {}).items():
        if a.get("status") == "ENTRY APPROACHING" and old.get(key) != "ENTRY APPROACHING":
            record("setup", symbol, f"Цена подходит к FVG-зоне (score {a.get('score')}) — готовлюсь к возможному входу", "info", now)
    if before.get("assessments") and after.get("zones") is not None:
        gone = set(before.get("assessments") or {}) - set(after.get("assessments") or {})
        if gone and any(old.get(k) in ("ENTRY APPROACHING", "POTENTIAL") for k in gone):
            record("invalidated", symbol, "Перспективная зона сломана или заполнена — снимаю с наблюдения", "muted", now)
    rv_b = (before.get("intelligence") or {}).get("relative_volume") or 0
    rv_a = (after.get("intelligence") or {}).get("relative_volume") or 0
    if rv_a >= 3 and rv_b < 3:
        record("volume", symbol, f"Всплеск объёма {rv_a:.1f}× от среднего", "warn", now)
    if (after.get("intelligence") or {}).get("regime") == "SHOCK" and (before.get("intelligence") or {}).get("regime") != "SHOCK":
        record("shock", symbol, "Резкий импульс волатильности (свеча 3+ ATR)", "warn", now)
    news_b = ((before.get("news") or {}).get("coin") or {})
    news_a = ((after.get("news") or {}).get("coin") or {})
    if news_a.get("critical") and not news_b.get("critical"):
        record("news", symbol, "Критичная новость: " + "; ".join(news_a.get("critical_titles") or [])[:200], "bad", now)
    elif news_a.get("headlines") and news_b.get("headlines") != news_a.get("headlines"):
        top = news_a["headlines"][0]
        if abs(top.get("tone") or 0) >= 0.5 and (not news_b.get("headlines") or news_b["headlines"][0].get("title") != top.get("title")):
            record("news", symbol, f"{'Позитивная' if top['tone'] > 0 else 'Негативная'} новость: {top['title'][:180]}", "good" if top["tone"] > 0 else "bad", now)


def format_brief(brief):
    pct = lambda x: f"{x*100:+.1f}%"
    lines = ["<b>Обзор рынка</b>", brief["summary"]]
    if brief["gainers"]:
        lines.append("Рост: " + ", ".join(f"{g['symbol']} {pct(g['change'])}" for g in brief["gainers"][:5]))
    if brief["losers"]:
        lines.append("Падение: " + ", ".join(f"{g['symbol']} {pct(g['change'])}" for g in brief["losers"][:5]))
    if brief["volume_spikes"]:
        lines.append("Всплески объёма: " + ", ".join(f"{v['symbol']} {v['rvol']:.1f}×" for v in brief["volume_spikes"][:5]))
    if brief["funding_high"]:
        lines.append("Перегретые лонги (funding): " + ", ".join(f"{f['symbol']} {f['funding']*100:.3f}%" for f in brief["funding_high"][:4]))
    if brief["news_bearish"]:
        lines.append("Негатив в новостях: " + ", ".join(n["symbol"] for n in brief["news_bearish"][:5]))
    if brief["news_bullish"]:
        lines.append("Позитив в новостях: " + ", ".join(n["symbol"] for n in brief["news_bullish"][:5]))
    stages = brief["stages"]
    lines.append(f"Сетапы: подходят к входу {stages.get('ENTRY APPROACHING', 0)}, потенциальные {stages.get('POTENTIAL', 0)}, развиваются {stages.get('DEVELOPING', 0)}.")
    return "\n".join(lines)
