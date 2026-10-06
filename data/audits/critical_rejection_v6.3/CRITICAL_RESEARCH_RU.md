# Критическое исследование V6.3 — исправление доказательств

## ROOT CAUSE

Предыдущий отчёт содержал ошибки агрегации. Его waterfall, FIRST BLOCKER и утверждение о 96,55% потерь нельзя использовать для принятия решений. Терминальная причина не является первым отказом за жизнь сетапа. Смешивались последние checks, любые отказы за всю историю и ever-entry. Все 18 entry-кандидатов ошибочно имели Rejection FAIL в той таблице.
Исходная база: 1654 ID; rejection когда-либо PASS=721, FAIL=931, ещё не достигнут=1249, UNKNOWN=4. Эти множества пересекаются во времени; складывать их нельзя.
Валидатор записывает первый отказ конкретного вызова. Записи после rejection доказывают его прохождение; отсутствие записи не доказывает PASS. Полные события и политики сохранены в archive_evidence.json.
V6.4 same-engine comparison: {'real_candidates': 18, 'first_execution_reasons': {'thin_orderbook': 15, 'higher_4h_opposition': 9, 'extended_from_vwap': 10, 'orderbook_unavailable': 1, 'higher_4h_unavailable_or_stale': 1, 'correlated_btc_opposition': 1}, 'counterfactual_gate_survivors': {'current': 0, 'vwap_soft': 1, 'book_soft': 3, 'both_soft': 8}, 'entry_reconstructed': 17, 'durable_states': {'WATCHLIST': 138, 'EXPIRED': 9, 'POTENTIAL': 82, 'INVALIDATED': 8, 'ENTRY APPROACHING': 8, 'DEVELOPING': 9}, 'cohorts': {'idealized': 144, 'actual_snapshot_detector_accepts': 10, 'classifications': {'EXPECTED_RULE_REJECTION': 134, 'PREVIOUS_RESEARCH_FALSE_NEGATIVE': 10}, 'volume_restored': 163}, 'historical_reference_context': {'mock_sent': 0, 'temporary_trades': 0, 'blockers': {'orderbook_unavailable': 16, 'higher_4h_opposition': 8, 'extended_from_vwap': 9, 'higher_4h_unavailable_or_stale': 1, 'noncontiguous_candles': 1}}}. Полная synthetic карточка: data/audits/v64_success_comparison/REFERENCE_SUCCESS_CASE.json; real18_cards.json и real18_timelines.json содержат одинаковые поля и causal time series. Historical execution context частично заменён явно помеченными mock book/funding/market данными; это не backtest.

## REJECTION AUDIT

trading/strategy.py: validate_signal. Берётся последняя строка уже закрытого отсортированного DataFrame. LONG: C[T] > O[T] AND C[T] >= CE. SHORT: C[T] < O[T] AND C[T] <= CE. Равенство close/open не проходит; равенство close/CE проходит.
До реакции требуется хотя бы один бар j в последних retest_window_bars с low[j] <= top AND high[j] >= bottom AND timestamp[j] > formed_at. balanced/contextual используют окно; legacy — один бар. contextual дополнительно допускает continuation при age<=2, но проверяет breakout/regime/relative-volume позже. Переменная rejection хранит последнюю touching candle для SL, но цвет и CE проверяются у последней свечи T, которая может отличаться от touching candle.
Wick ratio, engulfing, displacement и MSS не входят в production-AND реакции. Они измеряются только исследовательскими вариантами. Неверно говорить о 99,9% отказов detector по прежней таблице.
На входе engine вызывает decision_candles: сырой порядок/дубликаты/пропуски валидируются, затем остаются свечи с open_timestamp+duration<=observed_at. Структурный pivot получает правые бары только после их закрытия. validate_signal сам по себе не проверяет wall clock — гарантия находится в вызывающем коде. Индексы i+1 в FVG являются барами внутри уже известного префикса, не будущим относительно решения. Хранить timestamp открытия и время решения следует отдельно.
Из прежнего набора 1610 восстановлены 1466 ID с закрытыми OHLC-префиксами. Остальные — UNKNOWN, не FAIL. Всего по 1654 покрыты 1499.

| Reaction | Count | Production PASS | FAIL | N/A |
|---|---:|---:|---:|---:|
| wick | 489 | 223 | 266 | 0 |
| body | 1213 | 629 | 0 | 584 |
| close_ce | 0 | 0 | 0 | 0 |
| engulfing | 869 | 349 | 0 | 520 |
| displacement | 703 | 262 | 0 | 441 |
| mss_choch | 139 | 37 | 0 | 102 |
| multiple | 1040 | 524 | 0 | 516 |

Count — уникальный ID хотя бы с одним признаком; PASS приоритетен, если на одном из таких баров пройдена реакция. N/A — зона уже инвалидирована или нет актуального ретеста. Wick: touching candle, directional wick/range>=0.4, close за CE; body: directional close за CE; close_ce: пересечение CE относительно предыдущего close; engulfing: поглощение тела противоположной свечи; displacement: направленное тело>=предыдущий ATR14; MSS: новое CHOCH относительно прошлого префикса. Multiple — минимум два признака.

| Mode | Passed reaction+retest+validity | Technical proxy без полного market context | Potential | Confirmed |
|---|---:|---:|---|---|
| A | 629 | 33 | UNKNOWN | UNKNOWN |
| B | 489 | 53 | UNKNOWN | UNKNOWN |
| C | 629 | 33 | UNKNOWN | UNKNOWN |
| D | 262 | 0 | UNKNOWN | UNKNOWN |
| E | 37 | 0 | UNKNOWN | UNKNOWN |
| F | 725 | 73 | UNKNOWN | UNKNOWN |

A=current; B=wick; C=directional close; D=current AND displacement; E=current AND fresh CHOCH; F=wick OR current OR engulfing за CE OR CHOCH за CE. Остальной validate_signal переиспользован через offline AST-копию. Technical proxy использует 80 и частично восстановленный HTF; это не восстановление фактических 18 кандидатов. Исторические market breadth/book/news и полный профиль на каждой секунде недоступны. Поэтому нули Confirmed не подставляются вместо неизвестных исходов.

## LIQUIDITY AUDIT

Архив: ever sweep=136; ever no-sweep=1650; Entry без sweep=18. Следовательно, 27 — последний срез, а не число прошедших обязательный этап. Liquidity в balanced — информационный фактор, хотя влияет на developing diagnostics.
structure_context сравнивает последнюю свечу с последним подтверждённым swing low/high. Отсутствие sweep здесь не означает отсутствия sweep раньше. Detector не хранит историю снятия liquidity и не знает equal/session/day pools. Причины wrong timeframe/timestamp нельзя приписывать каждому false без доказательств; их статус UNKNOWN, а не установленная ошибка.
Старый expanded detector смешивал high и low в общем списке, затем проверял каждый уровень как оба типа liquidity. Его session window совпадало с коротким local window. Поэтому 1478/362 завышены/методологически непригодны. Новый research использует lows для sell-side и highs для buy-side, отдельные 20/50/288-bar pools, equal levels с допуском 0,1%, предыдущий UTC day и текущую UTC 8-hour session; проверяются только предшествующие уровни.
В reference-only части старой high-quality expanded группы восстановлено 323 зон; корректный typed sweep сохранился у 113. Это подмножество reference-only, а не все прежние 362.
Отдельно перепроверены все прежние 362 (включая production-зоны): {'count': 362, 'typed_sweep': 133, 'sweep_displacement_structure': 45, 'including_known_htf': 0, 'htf_unknown': 353}. Для всех 1623 старых Liquidity FAIL: {'rows': 1623, 'reasons': {'no_sweep_under_research_definition': 1091, 'expanded_pool_or_earlier_sweep_found': 381, 'INSUFFICIENT_DATA': 151}, 'pool_support': {'session': 219, 'equal_lows': 48, 'internal': 223, 'local': 114, 'previous_day': 133, 'external': 54, 'equal_highs': 16}}. Детали каждого ID и pool сохранены отдельно.

## FVG RECALL

Фиксированная выборка — те же 3462 reference-only ID из предыдущего snapshot. Предыстория расширена до 350 баров перед formation, чтобы ATR/volume не считались на обрезанных 30 барах. Цифры ниже описывают formation rules: текущую пригодность зоны отдельно определяют age, confirmations и mitigation. Они не сравнимы с 18 ночными entry ID из другого временного окна.
Первый отказ formation rules: {'volume': 914, 'body': 1298, 'atr': 640, 'formation_rules_pass': 485, 'size': 125}.
Качество по независимой причинной классификации: {'C': 3318, 'B': 144}. A=FVG+size+displacement+typed sweep+structure+known aligned HTF; B=то же без подтверждённого aligned HTF; C=условия этой строгой модели не выполнены. C не доказывает бесполезность.
Фактический detect_fvgs на полном 350-bar snapshot, с подтверждениями и lifecycle: {'count': 3462, 'reasons': {'volume': 914, 'body': 1298, 'atr': 640, 'accepted_with_full_warmup': 99, 'fvg_fully_filled': 322, 'lookback_or_confirmation_window': 54, 'size': 125, 'fvg_ce_invalidated': 10}}. 99 старых reference-only зон принимает неизменённый production detector при полноценном warm-up. Остаток formation PASS объясняется lifecycle либо lookback/confirmation window.
Idealized detector: 144 зон, категории {'B': 144}. HTF UNKNOWN у 3343 из всей выборки. Отсутствующие HTF нельзя считать opposite.
152 прежних 'strong body losses': найдено 152, quality={'C': 139, 'B': 13}; только body среди проверенных formation-фильтров=105. Прежнее слово 'только' было ошибочным: тогда считали body FAIL AND ATR>=1, не исключая другие отказы.
Body/ATR correlation=0.1638; overlap={'body_True_atr_True': 1524, 'body_False_atr_True': 437, 'body_True_atr_False': 640, 'body_False_atr_False': 861}. Body% измеряет долю тела в диапазоне самой свечи, ATR ratio — размер тела относительно прошлой волатильности. Общий числитель не делает эти фильтры тождественными.
649 прежних volume primary losses: найдено 649, после восстановления baseline={'FAIL': 486, 'PASS': 163}. Все reference-only volume states={'FAIL': 2156, 'PASS': 1306}.
Production при fvg_require_volume отвергает not finite baseline. Это подтверждено отдельным unit test. Нормализация volume/20 preceding volumes без impulse в baseline корректна по единицам внутри одного инструмента. Недостающая baseline должна диагностироваться DATA_UNAVAILABLE; пропуск торговли до данных допустим, подмена UNKNOWN на рыночный FAIL — нет. Нельзя суммировать base-volume разных монет; для межрыночных сравнений нужен quote turnover.

## SCORE AUDIT

Configured=80; settings loaded=80; effective generator=80; live dashboard/API policies=[80]. Class default=65. Оверрайда Score через environment mapping нет; DB перезаписывает paper_balance, а не score. Scheduler перечитывает тот же JSON и передаёт strategy_settings, сохраняющий порог.
Оригинальная база имеет несколько исторических policies: {'80': {'count': 17268, 'start': 1788983242.6234553, 'end': 1789013793.547253}, '50': {'count': 6, 'start': 1789012822.7768521, 'end': 1789012823.729099}, '60': {'count': 8, 'start': 1789012823.8199856, 'end': 1789012825.3276057}, '65': {'count': 357, 'start': 1789012825.2830484, 'end': 1789013715.739386}, '70': {'count': 11, 'start': 1789013257.255799, 'end': 1789013258.8951933}, '75': {'count': 13, 'start': 1789013258.9427934, 'end': 1789013740.0209486}}. Это факт, причина переключений по данным не установлена. Пользователь ранее явно выбрал сохранить 80. Все 18 кандидатов: bins={'below60': 0, '60-64': 0, '65-69': 0, '70-74': 0, '75-79': 0, '80+': 18}, thresholds={'80': 18}. Подробные баллы и восстановимые компоненты: candidate_scores.json; несовпадения окна/компонентов отмечены, не скрыты.

## MTF AUDIT

17 запрошенных candidate assets, данные срезаны на время исторического решения. Качественные зоны: {'1': 0, '5': 0, '15': 2, '60': 7, '240': 6}. Связок 15m→5m и 1h→15m с общей зоной, направлением и causal formation=2. Полные ranking/структура/границы в mtf_ranking.json. Это не охват всех монет, а обеспеченная данными выборка.

## FALSE NEGATIVES

В causal_replays.json: 30 сетапов, включая BLESSUSDT/MARSCOINUSDT/FARTCOINUSDT и 20 случайных ID (seed 6310). Для каждого доступного закрытия сохранены known prefix, реакция, инвалидирование и status. Будущие реакции reference-зон вынесены в future_descriptive_reaction и не участвуют в quality на formation.
Два исходных случайных ID без OHLC сохранены как UNKNOWN: ['ZILUSDT_5_1789002000000_BULLISH', 'AEONUSDT_5_1789011000000_BULLISH']. Добавлены два доступных случайных replacement (seed 6311), чтобы были 20 реальных random replays. Excursions следующих 12 баров сохранены отдельно в missed_opportunity_excursions.json; это не PnL.
Это кандидаты на missed opportunities, а не доказанные прибыльные пропуски. Доказать, какой фильтр лучше всего защищает качество, нельзя без сопоставимого out-of-sample outcome после издержек. Более частый геометрический PASS не означает более высокий winrate.

## TOP BOTTLENECKS / V6.4 PLAN

MUST FIX: исследовательские счётчики, сохранение per-call stage outcomes и policy/version; разнести terminal reason и first blocker; warm-up для baseline; типизация liquidity; отделить unavailable от market fail. Production пока не изменён.
SHOULD FIX: после frozen replay сравнить relative orderbook depth и нормализованную дистанцию VWAP как soft quality factors, сохраняя execution cost floors; исторический sweep с ограниченным возрастом; разнести context bonus и invalidation.
OPTIONAL: wick/combination reaction branch, 15m setup→5m entry, 1h→15m, отдельная reversal branch при opposite HTF+MSS. Проверять последовательно на сопоставимых входных данных.
DO NOT TOUCH: closed-candle causality, no look-ahead, full-fill/CE lifecycle действующей гипотезы, валидный SL/TP/RR, издержки и качество обязательных данных. Их прибыльная защитная сила пока не оценена, но отключать их ради числа сигналов оснований нет.
Минимальная исследовательская структура: подтверждённый причинный liquidity sweep → displacement → направленный трёхсвечный FVG → структура → валидная mitigation → объективная закрытая реакция. CONFIRMED дополнительно требует исполнимых цены/риска/стоимости и свежего контекста.
SIGNAL MAY FORM существует как diagnostic output: высокая structure quality и 1–2 отсутствующих условия. UNKNOWN HTF обозначается явно. Это не торговый сигнал. CLI debug: python scripts/critical_rejection_research.py --debug-setup <ID>. Telegram добавлены команды /why_not_signal <ID> и редкие уведомления NEW→IMPROVED→TRIGGER APPROACHING→INVALIDATED; paper trade и signal_outbox они не создают.
Expected effect: исправление отчёта меняет видимость, а не сигнализацию. F/typed liquidity/MTF могут увеличить обнаружение структур; эффект на Confirmed и winrate неизвестен. Risks: больше ложных wick reactions, слишком широкие equal levels, устаревшие sweep, MTF selection bias. Сначала проверка исходов и отдельный shadow dataset, затем решение о V6.4.

## Validation

33 новых теста rejection/indexing/UNKNOWN volume и 18 существующих pipeline tests пройдены. Дополнительно реальный engine на синтетической последовательности с предшествующим sweep, displacement, FVG и retest при threshold=80 дал Score=95, 1 Confirmed/Generated/Queued/Sent в mock Telegram, 1 paper trade во временной БД, 0 production trades. Результат synthetic_80.json. Сценарии A–F статуса исследовательской модели не означают гарантированного production signal. Production код/настройки не менялись.
