# Полный технический аудит FVG-бота — V6.3

Дата среза: 10 сентября 2026 UTC. Аудит выполнен в режиме read-only: исходники, порог Score и торговая политика не менялись. Версия зафиксирована как `research_v6.3`.

## Итог живой системы

Сервис жив, `is_running=true`, активных сделок нет, очередь доставки пуста. Последние проходы обрабатывали 410–411 выбранных пар; последний зафиксированный цикл обработал 410 из 410: 410 свежих, 0 stale, 0 failed, 0 skipped; длительность 27,45 с. Общий статус `WARNING`, потому что один обязательный HTF-ответ недоступен. Рабочие scanner, delivery, positions и system_status имеют свежий heartbeat.

Текущая политика: `scan_all_symbols=true`, эффективный LTF `5m`, контекст `60m`, оборот от 500 000, спред до 15 bps, профиль `balanced`, текущий Score threshold 80. Файл настроек и runtime policy согласованы; прошлые переключения 65↔80 сохранены в журнале и не смешаны с текущим срезом. Research worker имеет очередь 260 символов и один выполняемый запрос (`CVXUSDT`), режим gate — `paper`; это не блокирует live сигнал напрямую, но конкурирует за API и создаёт stale research reports после смены настроек.

## Ночная реконструкция

Окно 2026-09-09 19:47:20 — 2026-09-10 04:16:33 UTC: 17 667 наблюдений, 1 654 уникальных setup ID, 432 актива. Зафиксировано 1 650 FVG-проверок, 1 607 проверок пригодности зоны, 1 307 структурных проверок, 540 HTF-проверок, 136 liquidity-проверок и 18 технических кандидатов на вход. Сигналов создано, поставлено в outbox и отправлено: 0/0/0.

Главный отсев происходит до Telegram. Для всех наблюдений чаще всего встречались `no_recent_retest` (1 249; 75,51%), полное заполнение FVG (671; 40,57%), противоположный HTF (630; 38,09%), отсутствие направленного bearish rejection (539; 32,59%) и bullish rejection (392; 23,70%). Среди 18 кандидатов после технической валидации: thin orderbook 15 (83,33%), расширение от VWAP 10 (55,56%), противоположный 4h 9 (50%). Эти причины пересекаются, поэтому проценты не суммируются.

В архиве paper ledger 328 закрытых сигналов: 165 WIN и 163 LOSS, исторический win rate 50,3%. Это смешанные версии и режимы, поэтому показатель не является доказательством качества V6.3 и не подтверждает цель 70–75%.

## Архитектурный путь данных

`core/bybit.py` получает тикеры и закрытые свечи; `trading/engine.py` строит universe, ограничивает сетевые вызовы semaphore=5 и обрабатывает символы; `trading/strategy.py` обнаруживает FVG, считает индикаторы и вызывает валидацию; `trading/context.py` строит causal swing/BOS/CHOCH/sweep и trendline; `trading/intelligence.py` добавляет VWAP, benchmark, orderbook, OI и рыночную ширину; `setup_assessment.py` пишет наблюдение стадии; `signal_delivery.py` пишет durable outbox и пытается Telegram; `paper_trading.py` и `paper_account.py` ведут сделку и баланс; `telemetry.py` пишет pipeline events/scan cycles; `system_status.py` отправляет диагностическое сообщение, явно помеченное как не торговый сигнал.

Сканирование действительно охватывает сотни ликвидных символов. Поле `configured_symbols_count=30` — список ручных символов, а при `scan_all_symbols=true` фактический universe на текущем проходе равен 410.

## FVG и причинность

Реализовано классическое трёхсвечное правило: bullish gap, если `low[i] > high[i-2]` и средняя displacement-свеча закрылась выше открытия; bearish gap, если `high[i] < low[i-2]` и она закрылась ниже открытия. Проверяются минимальный размер, тело/диапазон, ATR displacement и, при включённом флаге, объём выше 20-свечной базы. CE — середина зоны. После формирования зона инвалидируется wick-fill за границу либо строгим закрытием за CE; касания и fill fraction считаются отдельно.

Индексы ограничены закрытым dataframe. Пивоты получают правые свечи только после их закрытия; backtest/replay передаёт prefix истории, поэтому в проверенном пути нового look-ahead не найдено. Тесты покрывают полное заполнение, CE, касания, несколько одновременных зон и повторную проверку перед отправкой.

Ограничение: независимого второго FVG-детектора в production-коде нет. Есть unit и replay проверки того же правила, но формального cross-detector diff для каждой исторической свечи пока нет; это остаётся пунктом следующего аудита.

## Структура, ликвидность и «психология»

`structure_context` использует подтверждённые swing high/low с pivot=3, строгий close-break и различает BOS от CHOCH по предыдущему направлению. Sweep проверяется на последней свече: прокол последнего swing и возврат закрытия внутрь. Displacement использует долю тела, диапазон, ATR и относительный объём. Контекст дополняется EMA50/EMA200, RSI, ADX, rolling VWAP, BTC correlation/beta, breadth, funding, OI и одним orderbook snapshot.

Модель ликвидности пока уже, чем полная SMC-реконструкция: нет равных максимумов/минимумов, пулов предыдущего дня/сессии, разделения internal/external liquidity и устойчивого подтверждения sweep несколькими свечами. Это не создаёт искусственных сигналов, но даёт false negatives и не позволяет честно заявить «анализируется абсолютно весь рынок».

News worker получает RSS/announcements и применяет keyword screening. В payload severity часто `unknown`; исторический backtest не содержит новостей, orderbook, funding и universe snapshots. Поэтому новости сейчас диагностические, а не проверенный причинный фильтр.

## Стадии и Score

Долговечные стадии: WATCHLIST → DEVELOPING → POTENTIAL → ENTRY APPROACHING → CONFIRMED, плюс INVALIDATED/EXPIRED и paper result. Проверки FVG, Zone eligibility, Structure, Liquidity, HTF bias и Entry записываются в payload. При этом отдельные durable статусы `Raw`, `Valid`, `Quality`, `TradeCandidate` не существуют как независимые события; их роль выполняют поля и причины.

Score начинается с 50 и добавляет FVG, LTF/HTF trend, ADX и RSI. Финальная валидация затем отдельно проверяет retest/rejection, relative volume, EMA, structure, VWAP, breadth, orderbook, 4h и риск. Поэтому Score 100 не означает разрешённый вход — это подтверждено текущими BEAT/NIULAI/ZAMA/STORJ/JASMY: высокие scores, но отсутствует последний закрытый rejection или есть execution blocker. `UNKNOWN` хранится как отсутствие данных и не трактуется как `FALSE` в telemetry, HTF и OI-пути.

## Производительность и отказоустойчивость

В журнале 30 последних циклов: длительность 24,21–55,37 с, среднее около 34,98 с; текущие циклы 27–32 с на 410 символов. Параллелизм ограничен пятью запросами. На символ сетевые вызовы идут ступенчато: LTF candles, затем при FVG HTF, 4h, orderbook/OI и research cache. Поле per-operation duration не сохраняется, поэтому честный top-10 медленных операций измерить по текущему ledger нельзя; доступны только cycle totals. Это измерительный пробел, а не скрытая оценка.

Запрошенный top-10 по архитектурному пути (ранжирование ожидаемой сетевой/IO стоимости, фактические миллисекунды не записываются):

1. LTF kline `/v5/market/kline` для каждого символа; 2. повтор kline после timeout; 3. HTF kline `60m`; 4. higher-4h kline; 5. orderbook limit=1000; 6. open-interest history; 7. research 5m history; 8. research 60m history; 9. ticker universe refresh; 10. Telegram send и SQLite outbox/telemetry commit. Первые восемь зависят от API latency и semaphore=5; последние два — от сети Telegram и блокировок SQLite. Для следующего аудита необходимы monotonic start/end на каждом вызове и p50/p95/p99 по символу и endpoint.

В durable telemetry 154 события: 76 `API_REQUEST_FAILED` для kline, 71 `ENTRY_CONTEXT_CHECK`, 6 `ENTRY_PRICE_REJECTED`, 1 `SYSTEM_STATUS_SENT`. Пустые свечи теперь считаются ошибкой, OI-ошибка не уничтожает валидный orderbook, stale/failed/skipped считаются на уровне цикла. Сейчас очередь доставки пуста.

В outbox остаются две старые строки со статусом `SENT`, но без `sent_at` и Telegram `message_id`. Новая схема хранит эти поля и attempts, однако для этих исторических отправок факт API-приёма независимо доказать нельзя; это ограничение журнала, а не текущая очередь.

Uvicorn переведён на `websockets-sansio`, что убрало прежние keepalive AssertionError. Однако supervisor log всё ещё содержит `RuntimeError: websocket.send after websocket.close`: dashboard loop ловит `WebSocketDisconnect`, но отдельная отправка может пересечь закрытие клиента. Это эксплуатационный дефект dashboard, не причина отсутствия Telegram-сигналов. Ticker refresh failure логируется общим scanner catch, но не имеет отдельного durable event.

## Текущие лучшие наблюдения

Срез последних 15 минут показывает пять `ENTRY APPROACHING`: BEATUSDT SHORT Score 100 (нет bearish rejection), NIULAIUSDT SHORT Score 100 (нет bearish rejection), ZAMAUSDT SHORT Score 100 (entry too far), STORJUSDT SHORT Score 100 (нет bearish rejection), JASMYUSDT SHORT Score 95 (нет bearish rejection и thin orderbook). Это watchlist/диагностика, не торговые приказы.

## Вердикт и план следующего аудита

Корневая причина нулевой ночной доставки: 18 кандидатов дошли до execution-контекста, затем были остановлены физическими/causal gates; Telegram не был единственным узким местом. Сканер сейчас работает, но система `WARNING` из-за одного обязательного HTF и остающихся WebSocket/API эксплуатационных пробелов.

Версия V6.3 заморожена. В этом аудите не снижался Score, не отключались фильтры, не добавлялись synthetic-сигналы и не перезапускалась стратегия. Следующий аудит по запросу должен добавить per-operation timing, независимый FVG diff, session/equal-liquidity reconstruction и классификацию всех 400→... переходов на одном и том же историческом окне.

Машиночитаемые факты: [FULL_TECHNICAL_AUDIT_V6.3.json](C:/Users/gekat/Desktop/fvgbot/data/audits/FULL_TECHNICAL_AUDIT_V6.3.json).
