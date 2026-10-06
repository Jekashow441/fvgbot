"""Render saved observations only; this script never generates or sends signals."""
import json
import sqlite3
from collections import Counter,defaultdict
from pathlib import Path

root=Path(__file__).resolve().parents[1]
folder=root/'data/audits'
status=json.loads((folder/'live_after_status.json').read_text(encoding='utf-8'))
market=json.loads((folder/'live_after_market.json').read_text(encoding='utf-8'))
history=json.loads((folder/'pipeline_after_report.json').read_text(encoding='utf-8'))
settings=json.loads((root/'data/bot_config.json').read_text(encoding='utf-8'))
con=sqlite3.connect((folder/'pipeline_after.db').as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
last={r['setup_id']:dict(r) for r in con.execute('SELECT * FROM setup_events WHERE id IN (SELECT max(id) FROM setup_events GROUP BY setup_id)')}
stages=Counter();diagnostics=Counter();candidates=[];groups=defaultdict(dict);expanded=0;detected_assets=set()
for asset,row in status['data'].items():
    diagnostics.update(row.get('setup_diagnostics',{}))
    for zone in row.get('zones',[]):
        assessment=row.get('assessments',{}).get(str(zone['candle_time'])+zone['type'],{})
        if not assessment:continue
        detected_assets.add(asset)
        key=f"{asset}_{settings['timeframe']}_{zone['candle_time']}_{zone['type']}"
        stage=last.get(key,{}).get('status',assessment['status']);stages[stage]+=1
        if stage in ('INVALIDATED','EXPIRED','MISSED','TP HIT','SL HIT'):continue
        candidates.append(dict(asset=asset,direction='LONG' if zone['type']=='BULLISH' else 'SHORT',score=assessment['score'],status=stage,current_price=row['current_price'],zone=zone,assessment=assessment,htf=row.get('htf_context'),blockers=row.get('blockers',[]),updated_at=row.get('updated_at')))
        if row.get('market',{}).get('turnover_24h',0)<5_000_000:expanded+=1
        corr=row.get('intelligence',{}).get('benchmark',{}).get('correlation')
        if corr is not None and corr>=.6:groups[str((row.get('processed_candle'),zone['type']))][asset]=round(corr,3)
candidates.sort(key=lambda r:(r['assessment']['signal_may_form'],r['status']=='ENTRY APPROACHING',r['status']=='POTENTIAL',r['status']=='DEVELOPING',r['score']),reverse=True)
summary=dict(scan=status['scan'],rows=len(status['data']),market_contracts=market['contracts'],eligible=market['eligible'],old_eligible_now=sum(r['turnover_24h']>=5_000_000 and r['spread_bps']<=settings['max_spread_bps'] for r in market['data'].values()),detected_assets=len(detected_assets),stages=dict(stages),diagnostics=dict(diagnostics),active_expanded_zone_count=expanded,top_candidates=candidates[:10],correlated_groups=dict(groups),signal_may_form=sum(r['assessment']['signal_may_form'] for r in candidates))
(folder/'live_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
total=sum(stages.values());eligible=summary['eligible']
lines=['# Текущий снимок расширенного аудита V6.2','',f"Последний завершённый проход: **{status['scan']['last_completed_at']}**, длительность {status['scan']['cycle_seconds']} с; затем пауза 15 с. Снимок API асинхронный: отдельные инструменты имеют своё updated_at. Это анализ состояния, не прогноз цены.",'',f"Рынок: {market['contracts']} контрактов с котировками → {eligible} проходят пороги оборота/спреда. При прежнем пороге 5 млн на тех же данных прошли бы {summary['old_eligible_now']}; дополнительно {eligible-summary['old_eligible_now']} инструментов. На добавленном сегменте наблюдаются {expanded} ещё не терминальных зон, включая Watchlist. Их прибыльность неизвестна.",'',f"Порог Score ≥{settings['min_signal_score']}; SIGNAL MAY FORM: {summary['signal_may_form']}. Статус этого аудита: WARNING.",'','| Текущая стадия | Зон | Доля от обнаруженных зон |','|---|---:|---:|']
for name in ('WATCHLIST','DEVELOPING','POTENTIAL','ENTRY APPROACHING','CONFIRMED','INVALIDATED','EXPIRED'):
    n=stages[name];lines.append(f'| {name} | {n} | {n/total*100:.2f}% |' if total else f'| {name} | {n} | N/A |')
lines += ['',f"Обнаружено {total} зон на {len(detected_assets)} инструментах ({len(detected_assets)/eligible*100:.1f}% от охвата). Доли стадий — распределение текущих состояний; они не являются вероятностями последовательных переходов. Зона может сразу появиться как Potential. Текущие Confirmed/открытые paper: {stages['CONFIRMED']}/{status['stats']['active_count']}.",'','## Историческая воронка и переходы','', 'Исходная копия до исправления: расширенный охват 361 на момент его настройки → 1364 наблюдавшихся сетапа → 650 когда-либо Potential → 1 Confirmed → 1 Paper → 1 Closed. Доля Potential от наблюдавшихся — 47.65%; Confirmed от всех — 0.073%; единственный Confirmed был ранее Potential (1/650 = 0.154%). После Confirmed 1/1 дошёл до paper и закрытия; это один случай. Developing и Entry Approaching в той версии не записывались: переходы через них UNKNOWN. Инструменты и накопленные за часы сетапы — разные единицы счёта.','', 'Фактически записанные переходы статусов после внедрения новых стадий и до итоговой копии (число событий, с возможными повторными переходами):','']
lines += [f'- {key}: {value}' for key,value in history['observed_status_transitions'].items()]
lines += ['','## Первоначальные оценки новых и исторических наблюдений','','Группировка строго по первой записи setup_id. UNKNOWN не заполняется поздним score.','', '| T1 Score | Сетапов | Confirmed | Invalidated | Expired | Paper | WIN/LOSS | Средний RR | FPR |','|---|---:|---:|---:|---:|---:|---|---|---|']
for label,g in history['initial_score_bands'].items():
    lines.append(f"| {label} | {g['count']} | {g['confirmed']} | {g['invalidated']} | {g['expired']} | {g['paper_trades']} | {g['wins']}/{g['losses']} | {g['mean_confirmed_rr'] if g['mean_confirmed_rr'] is not None else 'N/A'} | UNKNOWN |")
lines += ['','## Актуальные кандидаты','','Ранний диагностический Score до обучения/всех ограничений. Высокое значение не разрешает вход. Рейтинг учитывает стадию; он не ранжирует ожидаемую доходность.','']
for r in candidates[:8]:
    z=r['zone'];a=r['assessment'];long=r['direction']=='LONG';ce=z['ce'];boundary=z['bottom'] if long else z['top'];comp='ниже' if long else 'выше'
    lines += [f"### {r['asset']} {r['direction']} — {r['status']}, Score {r['score']}",'',f"TF {settings['timeframe']}m; наблюдение {r['updated_at']}; Current price {r['current_price']}; закрытая цена анализа {a['current_price']}. FVG [{z['bottom']}, {z['top']}], CE {ce}; HTF {(r['htf'] or {}).get('trend','UNKNOWN')}; структура {a['structure']}.",'',f"Проверено: {a['checks']}. Первый отказ и постоянные ограничения зоны: {', '.join(a['rejections']) or 'нет раннего отказа'}. Внешние blockers: {r['blockers'] or 'на этом этапе не обнаружены; подробный стакан/4h может ещё не проверяться' }.",'',f"Условие Potential: HTF в направлении сделки, структура в этом направлении или RANGE, зона ещё пригодна, нет блокирующего контекста. Сейчас: {r['status']}.",'',f"Для Confirmed: ретест зоны после её формирования в пределах {settings['retest_window_bars']} закрытых баров; направленная свеча с close {'выше' if long else 'ниже'} CE {ce}; relative volume ≥{settings['min_relative_volume']}; расстояние ≤{settings['max_entry_distance_atr']} ATR; допустимые касания, 4h/BTC/новости/стакан, RR после издержек и риск; итоговый Score ≥{settings['min_signal_score']}. Последующие проверки могут дать дополнительные отказы.",'',f"Отмена: полное заполнение до {boundary} или дальше; запрещённое закрытие {comp} CE; истечение срока. Превышение {settings['fvg_max_touch_bars']} touch bars запрещает вход этой зоны. Entry/SL/TP/вероятность победы: INSUFFICIENT DATA до подтверждения.",'']
lines += ['## Что ограничивает входы','', 'Счётчики первых причин отказа стратегии (не независимые события и не FPR):','']+[f'- {k}: {v}' for k,v in diagnostics.most_common()]
lines += ['','## Общий рыночный фактор','','Инструменты ниже имеют корреляцию к BTC ≥0.6 и одинаковое направление FVG/анализируемый бар. Это диагностические группы, не доказанные дубли и не новые ограничения.','']
for key,assets in groups.items():
    if len(assets)>1:lines.append(f'- {key}: {len(assets)} уникальных инструментов — '+', '.join(f'{s} ({v})' for s,v in assets.items()))
lines += ['', '**WARNING**',f"361 (исторический охват) → {eligible} сейчас → {total} зон → {stages['POTENTIAL']} Potential → {stages['ENTRY APPROACHING']} Entry Approaching → {stages['CONFIRMED']} Confirmed.",'', 'Топ кандидатов приведён выше; SIGNAL MAY FORM требует отдельного подтверждённого снятия ликвидности и сейчас не приравнивается к числу Potential. Главный технический дефект исправлен: раздельное сохранение paper-закрытия и баланса допускало повторное начисление после сбоя. Следующий шаг по запросу — сопоставимый shadow-replay пропущенных ретестов и диапазона 65–79 с неизменным рабочим порогом 80.']
(root/'PIPELINE_LIVE_V6.2.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({k:summary[k] for k in ('scan','eligible','old_eligible_now','detected_assets','stages','diagnostics','signal_may_form')},ensure_ascii=True))
