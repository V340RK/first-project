# SMC Deep Dive — Технічний довідник для бота

> Конспект з книги "Smart Money" (195 сторінок).
> Структуровано як алгоритмічний довідник, а не як навчальний матеріал.
> Кожна концепція містить: визначення → правила виявлення → дії бота.

---

## Зміст

1. [Базова структура ринку](#1-базова-структура-ринку)
2. [Ліквідність](#2-ліквідність)
3. [Підтримка і опір (S/R)](#3-підтримка-і-опір-sr)
4. [Premium / Discount / Equilibrium](#4-premium--discount--equilibrium)
5. [POI (Point of Interest)](#5-poi-point-of-interest)
6. [IMB / FVG](#6-imb--fvg)
7. [SFP (Swing Failure Pattern)](#7-sfp-swing-failure-pattern)
8. [Order Block (OB)](#8-order-block-ob)
9. [Breaker Block (BB)](#9-breaker-block-bb)
10. [FTA (First Trouble Area)](#10-fta-first-trouble-area)
11. [Патерни: QM, 3DP, Reversal/Continuation, PO3](#11-патерни)
12. [Range](#12-range)
13. [Торгівля по тренду](#13-торгівля-по-тренду)
14. [Свічкові моделі: STB, BTS, SC, Wick](#14-свічкові-моделі)
15. [Demand & Supply zones](#15-demand--supply-zones)
16. [Mitigation, DP, RTO, Origin](#16-mitigation-dp-rto-origin)
17. [POI підтвердження: CHoCH, VSR, Flip, Inducement](#17-poi-підтвердження)
18. [Wyckoff](#18-wyckoff)
19. [Market Maker Models: MMBM, MMSM](#19-market-maker-models)
20. [Сесії і KillZones](#20-сесії-і-killzones)
21. [Confluence Checklist для бота](#21-confluence-checklist)

---

## 1. Базова структура ринку

### Терміни:
- **HH** (Higher High) — вищий максимум
- **HL** (Higher Low) — вищий мінімум
- **LH** (Lower High) — нижчий максимум
- **LL** (Lower Low) — нижчий мінімум
- **BOS** (Break of Structure) — пробій структури
- **Conf** (Confirmation) — підтвердження
- **Swing High/Low** — локальний max/min
- **Strong High/Low** — сильний (захищається), **Weak High/Low** — слабкий (легко пробивається)
- **Impulse** — сильний односторонній рух, **Correction** — відкат

### Фази ринку:
- **Uptrend**: HH + HL послідовно
- **Downtrend**: LH + LL послідовно
- **Accumulation**: після падіння, боковик, SM накопичує
- **Distribution**: після росту, боковик, SM розподіляє (продає)

### BOS — пробій структури:
```
Бичачий BOS: close > попередній swing high
  → тренд вгору підтверджено
  → далі очікуємо новий HL

Ведмежий BOS: close < попередній swing low
  → тренд вниз підтверджено
  → далі очікуємо новий LH
```

### Правило бота:
> Торгуємо ТІЛЬКИ в напрямку підтвердженого BOS на старшому TF.
> Якщо BOS немає — ринок у ренжі, торгувати за стратегією "в тренді" заборонено.

---

## 2. Ліквідність

### Визначення:
Ліквідність — області де сконцентровані стоп-лоси і лімітні ордери. **Ринок рухається туди де є ліквідність.**

### Типи:
| Тип | Що це |
|---|---|
| **Buy-side Liquidity (BSL)** | Стопи шортистів над swing high, buy-stop ордери |
| **Sell-side Liquidity (SSL)** | Стопи лонгістів під swing low, sell-stop ордери |
| **Internal Liquidity** | Ліквідність всередині останнього розмаху |
| **External Liquidity** | Ліквідність за межами останнього розмаху |
| **EQH/EQL** | Equal Highs/Lows — два+ однакові max/min |
| **Order Book Liquidity** | Великі лімітні ордери в DOM |

### Sweep liquidity (зачистка):
Ціна короткочасно пробиває рівень (бере стопи), потім розвертається.
```
Ознаки SSL sweep:
  1. Ціна пробила swing low вниз
  2. Закрилась ВИЩЕ swing low (повернулась в діапазон)
  3. Після цього — імпульс вгору
  → Сигнал Long
```

### Правило бота:
> EQH/EQL завжди розглядаємо як **магніт** для ціни.
> Після того як ліквідність забрана — очікуємо рух у протилежному напрямку.
> **Внутрішня ліквідність забирається до того як ціна йде до зовнішньої.**

---

## 3. Підтримка і опір (S/R)

### Класичні поняття:
- **Support** — рівень де продажі "закінчуються" і ціна відскакує вгору
- **Resistance** — рівень де покупки "закінчуються" і ціна відскакує вниз
- **Flip** — коли support перетворюється на resistance (або навпаки) після пробою

### Формула сили рівня:
```
Сила = (к-ть тестів) × (об'єм на рівні) × (час між тестами)

S/R стає сильним якщо:
  ≥ 2-3 тестів з відскоком
  Високий об'єм на дотику
  Формується поступово (100-150 тіків від зони)
```

### Помилка новачка: торгувати на кожному рівні.
**Правило бота**: торгуємо S/R ТІЛЬКИ в поєднанні з іншими сигналами (POI, OB, confluence).

---

## 4. Premium / Discount / Equilibrium

### Визначення:
Діапазон між останнім swing high і swing low ділиться на зони:
- **Discount** — нижня половина (0–0.5) → **зона для LONG**
- **Premium** — верхня половина (0.5–1.0) → **зона для SHORT**
- **Equilibrium** — рівень 0.5 (середина)

### Правила:
```
Купуємо в Discount, продаємо в Premium.

НЕ КУПУЄМО в Premium — це дорого.
НЕ ПРОДАЄМО в Discount — це дешево.

Коли ціна перетинає 0.5 — це зона байас-відрахунку,
тестує силу обох сторін.
```

### Алгоритм:
```python
def zone(price, swing_low, swing_high):
    mid = (swing_low + swing_high) / 2
    if price < mid:
        return "discount"   # можливі лонги
    elif price > mid:
        return "premium"    # можливі шорти
    else:
        return "equilibrium"
```

---

## 5. POI (Point of Interest)

### Визначення:
**POI** — ціновий рівень або зона де ймовірна реакція ціни. Не сигнал для входу сам по собі — потрібне підтвердження.

### Типи POI:
- Swing high/low (зона ліквідності)
- Ліквідність (EQH/EQL)
- S/R рівень
- Premium/Discount 0.5
- **Order Block** (див. розділ 8)
- **FVG/IMB зона** (див. розділ 6)
- Fibonacci 0.62, 0.705, 0.79 (OTE — Optimal Trade Entry)

### Правило бота:
> POI сам по собі — НЕ сигнал на вхід.
> Вхід тільки після **підтвердження** (CHoCH, SFP, BOS на LTF всередині POI).

---

## 6. IMB / FVG

### Imbalance (IMB):
Коли за дуже короткий час (1-2 свічки) ціна рухається так швидко, що залишається **"порожнина"** — зона без двосторонньої торгівлі.

### Fair Value Gap (FVG):
IMB на рівні ≥ 0.5 (суттєвого розміру). Також званий "зоною справедливої ціни".

### Алгоритм виявлення:
```python
def find_fvg(candles):
    fvgs = []
    for i in range(1, len(candles) - 1):
        # Бичачий FVG: пропуск між high[i-1] і low[i+1]
        if candles[i-1].high < candles[i+1].low:
            # Перевіряємо розмір імпульсу — має бути сильна свічка [i]
            impulse_size = abs(candles[i].close - candles[i].open)
            avg_range = average_range(candles, window=20)
            if impulse_size >= avg_range * 1.5:
                fvgs.append({
                    'type': 'bullish',
                    'top': candles[i+1].low,
                    'bottom': candles[i-1].high,
                    'mid': (candles[i+1].low + candles[i-1].high) / 2
                })

        # Ведмежий FVG
        if candles[i-1].low > candles[i+1].high:
            impulse_size = abs(candles[i].close - candles[i].open)
            if impulse_size >= avg_range * 1.5:
                fvgs.append({
                    'type': 'bearish',
                    'top': candles[i-1].low,
                    'bottom': candles[i+1].high,
                    'mid': (candles[i-1].low + candles[i+1].high) / 2
                })
    return fvgs
```

### Типи заповнення:
- **Partial fill** — ціна зайшла в FVG частково
- **Full fill** — ціна повністю заповнила FVG (прийшла до протилежного краю)

### Правило бота:
> FVG/IMB заповнюються в ~70% випадків.
> Використовуємо FVG як POI для пошуку входу (в напрямку імпульсу який створив FVG).
> **Вхід** — на краю FVG (0.5 рівень IMB), з підтвердженням.

---

## 7. SFP (Swing Failure Pattern)

### Визначення:
Свічка пробиває swing high/low, але **закривається назад** у діапазоні. Це "фейк пробій".

### Ознаки сильного SFP:
```
1. Довгий wick (хвіст) за межі swing level
2. Висока relative volume (об'єм > середнього)
3. Наступна свічка підтверджує — йде в протилежному напрямку
```

### Алгоритм:
```python
def detect_sfp(current_candle, swing_level, direction='low'):
    if direction == 'low':  # SFP під swing low (бичачий)
        return (current_candle.low < swing_level
                and current_candle.close > swing_level
                and (current_candle.close - current_candle.low) /
                    (current_candle.high - current_candle.low) > 0.6)
    else:  # SFP над swing high (ведмежий)
        return (current_candle.high > swing_level
                and current_candle.close < swing_level
                and (current_candle.high - current_candle.close) /
                    (current_candle.high - current_candle.low) > 0.6)
```

### Правило бота:
> SFP дає високу ймовірність розвороту.
> Працює найкраще на старших TF (1H+).
> Вхід після SFP: ринок увійшов в swing рівень, провалився, закрився назад → Long/Short у зворотному напрямку.
> Стоп: за екстремумом wick.

---

## 8. Order Block (OB)

### Визначення:
**Order Block** — остання свічка **протилежного** кольору перед сильним імпульсним рухом який порушив структуру.

### Бичачий OB:
```
Остання ВЕДМЕЖА свічка перед сильним бичачим імпульсом + BOS вгору.
Зона OB = [low, high] цієї ведмежої свічки.
```

### Ведмежий OB:
```
Остання БИЧА свічка перед сильним ведмежим імпульсом + BOS вниз.
Зона OB = [low, high] цієї бичачої свічки.
```

### Три ознаки валідного OB:
1. **Absorption** — поглинання в зоні OB (велика активність)
2. **IMB** створено після OB свічки (імпульс)
3. **OTE** (Optimal Trade Entry) — ціна повернулась до зони в межах Fib 0.62-0.79

### Алгоритм пошуку:
```python
def find_bullish_ob(candles, bos_index):
    # Шукаємо від BOS назад до першої ведмежої свічки
    for i in range(bos_index - 1, max(0, bos_index - 10), -1):
        if candles[i].close < candles[i].open:  # ведмежа
            # Перевірка: після неї був сильний імпульс?
            impulse_size = candles[bos_index].high - candles[i].low
            if impulse_size > atr(candles) * 3:
                return {
                    'zone_low': candles[i].low,
                    'zone_high': candles[i].high,
                    'zone_50': (candles[i].low + candles[i].high) / 2,
                    'zone_30': candles[i].low + (candles[i].high - candles[i].low) * 0.3
                }
    return None
```

### Вхід по OB:
- **Лімітка** на 0.3 зони OB (вхід з підтвердженням в зоні 0.3-0.5)
- **Стоп** — під OB мінімум (для лонгу) + буфер 1-2 тіки
- **Тейк** — до наступного POI або за RR мінімум 2:1

### Правило бота:
> 90% OB сценаріїв потребують **підтвердження** на LTF (SFP, BOS + Retest, CHoCH всередині зони).
> Входимо не на першому торкненні, а після того як ціна реагує в зоні.

---

## 9. Breaker Block (BB)

### Визначення:
Breaker Block — це **OB який був пробитий**, і тепер працює в **протилежному** напрямку.

### Логіка:
```
1. Був бичачий OB (зона купівлі)
2. Ціна пробила його вниз
3. Тепер ця сама зона стає РЕЗИСТЕНСОМ (продажі)
4. Коли ціна повертається до цієї зони — шортимо
```

### Ознаки:
- OB пробитий
- Є IMB (імпульс) після пробою
- Ціна повернулась на тест тепер-пробитого OB
- На тесті є LTF підтвердження (SFP/CHoCH)

### Правило бота:
> BB часто сильніший сигнал ніж перший тест OB — бо структура вже змінилась.

---

## 10. FTA (First Trouble Area)

### Визначення:
**FTA** — перша перешкода між точкою входу і тейком, яка може завадити руху до цілі.

### Приклади FTA:
- OB на шляху до тейку
- FVG який не заповнений
- Swing high/low між входом і тейком
- BB в середині руху

### Правило бота:
```
ПЕРЕД входом в угоду:
  1. Визначити ціль (тейк)
  2. Знайти всі POI між входом і тейком (FTA)
  3. Якщо FTA є → або скоротити тейк до FTA, або збільшити ризик прорахунку
  4. Якщо FTA зона сильна → НЕ входимо (малий потенціал руху)
```

---

## 11. Патерни

### Quasimodo (QM):
- Фаза 1: розворотний патерн (Wick/SFP)
- Фаза 2: зібрана ліквідність (sweep)
- Фаза 3: BOS в протилежному напрямку
- Вхід: після BOS, на POI (OB/FVG), у напрямку BOS

### Three Drives Pattern (3DP):
- 3 послідовні хвилі в одному напрямку
- Кожна наступна слабша (по об'єму і довжині)
- Часто супроводжується дивергенцією RSI
- Вхід: після 3-го драйву, розворот у зворотному напрямку

### Reversal & Continuation:
- **Reversal** = ринок змінює напрямок після ліквідності + BOS у новому напрямку
- **Continuation** = ринок відновлює тренд після корекції, BOS у напрямку тренду
- На корекції — часто SFP + OB/FVG

### Power of Three (PO3 / AMD):
Модель з 3 фаз:
1. **Accumulation (Consolidation)** — боковик, формується діапазон, ліквідність по обидва боки
2. **Manipulation** — false breakout — забирає ліквідність в одну сторону
3. **Distribution** — реальний рух у протилежному напрямку після маніпуляції

```
Приклад PO3 вгору:
  Фаза 1: Ціна у діапазоні 50,000-50,500 (накопичення)
  Фаза 2: Ціна провалилась до 49,800 (забрала SSL), 
          потім швидко повернулась назад
  Фаза 3: Імпульсний рух вгору до 51,000+
```

### Правило бота:
> PO3 — одна з найсильніших моделей. Особливо працює на **NY Midnight (07:00 за Києвом)** як точці старту денного циклу.

---

## 12. Range

### Визначення:
Range — боковий рух між чіткими high і low. Ринок 70-80% часу в ренжі.

### Зони всередині Range:
```
1.0 (top)    ← Premium ← зона для шортів
...
0.5 (mid)    ← Equilibrium ← зона невизначеності
...
0.0 (bottom) ← Discount ← зона для лонгів
```

### Торгівля в Range:
```
Правила:
  1. Купуємо біля low (0-0.3), продаємо біля high (0.7-1.0)
  2. НЕ входимо в середині (0.3-0.7) — це зона невизначеності
  3. Тейк — протилежна межа ренжу (або 50%)
  4. Стоп — за межею ренжу

УВАГА: перед виходом з ренжу часто відбувається sweep (забір ліквідності
за EQH/EQL). Це — можливість для входу, а не привід для паніки.
```

### Вихід з Range:
- **Breakout** підтверджується BOS на старшому TF
- Часто після breakout ціна повертається на retest межі ренжу
- Найкращий вхід по тренду — **після retest** пробитої межі

---

## 13. Торгівля по тренду

### Основні принципи:
1. Визначити тренд на HTF (1H, 4H)
2. Шукати вхід на LTF (M5, M15) при корекції в тренді
3. Корекція — не проста: часто **Complex Pullback** (CPB) — подвійна корекція з sweep ліквідності

### Strong vs Weak swing:
- **Strong swing** — зона яка захищається, ціна розгортається від неї
- **Weak swing** — зона яка буде пробита, ліквідність за нею забрана

### SMS (Shift in Market Structure):
Коротка зміна структури яка **не означає розворот** тренду.
Часто це спосіб SM забрати ліквідність всередині корекції.

### Правило бота:
```
У тренді ВГОРУ:
  - Чекаємо корекцію до HTF POI (OB, FVG)
  - На LTF чекаємо SFP/CHoCH/BOS вгору
  - Вхід LONG з підтвердженням
  - Стоп під POI

НЕ ТОРГУЄМО:
  - Проти тренду HTF
  - Якщо POI не підтверджений на LTF
  - Під час важливих новин
```

---

## 14. Свічкові моделі

### STB (Sell to Buy):
Модель розвороту вгору:
1. Ціна робить імпульс вниз
2. Потім імпульс вгору (BOS)
3. Зона розвороту — на 0.5 між low і high розвороту
4. Часто супроводжується **absorption** на дні

### BTS (Buy to Sell):
Дзеркально — розворот вниз:
1. Імпульс вгору
2. Імпульс вниз (BOS)
3. Зона розвороту — 0.5 між high і low

### Sponsored Candle (SC):
Імпульсна свічка яка створює IMB/FVG. Ознака що крупний гравець увійшов.
```
Ознаки SC:
  - Тіло свічки > 60% загального діапазону
  - Volume > 1.5× середнього
  - Закриття близько до екстремуму (wick < 20% тіла)
  - Створює FVG

SC дає ціль: повернення до 0.3-0.5 зони SC свічки
```

### Wick (хвіст):
Довгий wick — часто SFP. Працює як міні-STB/BTS.
Wick + SC поряд — сильніший сигнал чим кожен окремо.

### Order Flow:
Послідовність інтерпретації:
- Послідовні HL на LTF = бичачий Order Flow
- Послідовні LH на LTF = ведмежий Order Flow
- Зміна Order Flow = сигнал розвороту на LTF

---

## 15. Demand & Supply zones

### Відмінність від Order Block:
- **OB** — конкретна свічка
- **Demand/Supply zone** — ширша область (може включати кілька свічок)

### Demand zone (попит — зона покупок):
```
Компоненти:
  - Liquidity — накопичена ліквідність (sell-side)
  - Origin — точка звідки почався імпульс
  - DP (Decision Point) — точка прийняття рішення (найактивніша зона)
  - BOS — імпульс вгору після зони

Зона працює коли ціна повертається → очікуємо реакцію.
```

### Supply zone (пропозиція — зона продажів):
```
Компоненти:
  - Liquidity (buy-side)
  - Origin
  - DP
  - BOS вниз після зони
```

### Правило бота:
> Demand/Supply використовуємо на HTF для визначення напрямку.
> Вхід на LTF з підтвердженням всередині зони.

---

## 16. Mitigation, DP, RTO, Origin

### Mitigation:
Повернення ціни в зону D/S для того щоб "заповнити ордери" які залишились.

### Origin (RTO — Return To Origin):
Точка **початку** імпульсного руху. Зазвичай — сильна зона реакції.

### Decision Point (DP):
Точка всередині зони D/S де крупний гравець прийняв рішення входити. Часто — 0.3-0.5 від зони.

### Алгоритм:
```
1. Знайти Demand/Supply зону з BOS
2. Позначити Origin і DP
3. Чекати mitigation (повернення ціни в зону)
4. Вхід біля DP з підтвердженням
5. Стоп за Origin
6. Тейк — до наступної D/S зони або за Fibonacci
```

---

## 17. POI підтвердження

Це **критично** для скальпінга — сам POI недостатньо, потрібен тригер.

### CHoCH (Change of Character):
- Міні-BOS проти останнього локального тренду
- На 5M/15M — для LTF підтвердження
- На 1M — для ultra-fast входу

```
Бичачий CHoCH:
  - Був downtrend (LL, LH)
  - Раптом ціна закрилась ВИЩЕ останнього LH
  → Сигнал зміни локального тренду на бичачий
```

### VSR (V-shape Recovery):
V-подібне відновлення після CHoCH.
```
1. Ціна зайшла в POI
2. CHoCH сформувався
3. VSR — різкий розворот з мінімальною корекцією
→ Вхід на VSR
```

### Flip:
Зміна зони D ↔ S (Demand to Supply або Supply to Demand).
- **D2S flip** — демонд зона перетворилась на сапплай (тепер резистенс)
- **S2D flip** — сапплай зона стала демондом

### Inducement:
Ліквідність ПЕРЕД POI. SM спочатку забирає inducement (створює false move), потім йде до реального POI.

```
Приклад:
  POI на $50,000 (OB)
  Inducement на $50,200 (маленький LH)
  Ціна: піднімається до $50,200 (бере inducement), 
        потім опускається до $50,000 (POI)
  → Тільки на POI шукаємо вхід, не раніше
```

### Risk Entry vs Confirmation Entry:
| Entry | Коли | Ризик |
|---|---|---|
| **Risk Entry** | Вхід на досягненні POI без підтвердження | Вищий, RR краще |
| **Confirmation Entry** | Вхід після CHoCH/SFP/Flip в POI | Нижчий, RR гірше |

**Правило бота**: використовуємо **тільки Confirmation Entry** (ризик-менеджмент № 1).

---

## 18. Wyckoff

### Концепція:
Ринок проходить через 4 фази:
1. **Accumulation** — накопичення (SM купує)
2. **Markup** — рух вгору
3. **Distribution** — розподіл (SM продає)
4. **Markdown** — рух вниз

### Ключові події Accumulation:
- **PS** — Preliminary Support (попередня підтримка)
- **SC** — Selling Climax (кульмінація продажів)
- **AR** — Automatic Rally (автоматичний відскок)
- **ST** — Secondary Test (вторинний тест)
- **Spring** — false breakout вниз (shakeout)
- **Test** — тест Spring
- **SOS** — Sign of Strength (сигнал сили)
- **LPS** — Last Point of Support (остання підтримка)
- **BU** — Back-up

### Ключові події Distribution:
- PS, BC (Buying Climax), AR, ST
- **UT** — Upthrust (shakeout вгору)
- **UTAD** — Upthrust After Distribution
- **MSOW** — Major Sign of Weakness
- **LPSY** — Last Point of Supply

### Re-accumulation/Re-distribution:
Проміжна фаза коли ринок консолідується в тренді перед продовженням.

### Fake distribution:
Схема яка виглядає як distribution але насправді є re-accumulation.
**Signal**: SOS після UTAD замість MSOW.

### Правило бота:
> Wyckoff складна модель для скальпінга — використовуємо як **HTF контекст**.
> Якщо на 4H схема Accumulation і ми в фазі LPS/Spring — шукаємо лонги на LTF.

---

## 19. Market Maker Models

### MMBM (Market Maker Buy Model):
Модель коли SM формує довгу позицію.
```
Фази:
  1. Original Consolidation — накопичення
  2. SSL Sweep — забір ліквідності ВНИЗ (shakeout)
  3. BOS/MSB вгору — пробій структури
  4. Retest POI (IMB, PDL)
  5. Ціль: BSL (PDH, PWH — Previous Day/Week High)
```

Часто старт на **NY Midnight (07:00 за Києвом)**.

### MMSM (Market Maker Sell Model):
Дзеркально — модель формування шорт позиції.
```
Фази:
  1. Original Consolidation
  2. BSL Sweep — забір ліквідності ВГОРУ
  3. BOS/MSB вниз
  4. Retest POI
  5. Ціль: SSL (PDL, PWL)
```

### Структура бота:
```python
def detect_mmbm(data, timeframe='1H'):
    # 1. Чи є консолідація останні N годин?
    consolidation = is_consolidating(data, hours=6)
    
    # 2. Чи був sweep SSL?
    ssl_swept = detect_ssl_sweep(data)
    
    # 3. Чи є BOS вгору після sweep?
    bos_up = detect_bos(data, direction='up', after=ssl_swept.time)
    
    # 4. Чи ціна на retest POI?
    at_poi = price_at_poi(data.current_price)
    
    if consolidation and ssl_swept and bos_up and at_poi:
        return {'signal': 'long', 'target': next_bsl_level(data)}
    return None
```

---

## 20. Сесії і KillZones

### Сесії (UTC+3 Київ):
- **Азіатська (Tokyo)**: 02:00 – 10:00 → слабка волатильність, формується діапазон
- **Лондонська**: 10:00 – 18:00 → висока волатильність, London open 10:00
- **Нью-Йоркська**: 15:00 – 23:00 → найвища волатильність 
- **Lunch Time**: 14:00 – 15:00 → низька активність, часто boxy rangey

### KillZones (найкращий час для торгівлі):
- **London KillZone**: 09:00 – 12:00 UTC+3 — запуск тижневого/денного руху
- **NY KillZone**: 15:00 – 18:00 UTC+3 — продовження або розворот
- **NY Midnight**: 07:00 UTC+3 — старт денного циклу, часто запуск MMBM/MMSM

### Правило бота:
```
ТОРГУЄМО:
  - London KillZone (09:00-12:00)
  - NY KillZone (15:00-18:00)
  - NY Midnight (07:00-08:00) — для MM моделей

НЕ ТОРГУЄМО:
  - Азіатська сесія (низька волатильність, багато false signals)
  - Lunch Time (14:00-15:00)
  - За 30 хв до / 30 хв після важливих новин (див. forexfactory.com)
```

### Новинний календар:
- **Red news** — критичні (NFP, FOMC, CPI) → не торгуємо ±30 хв
- **Orange news** — важливі (GDP, Retail Sales) → обережно
- **Yellow news** — низький вплив
- **Звіти** — квартальні звіти великих компаній

---

## 21. Confluence Checklist

Фінальний чек-лист який бот використовує перед входом в угоду.

### Мінімум 5 з 7 мають бути виконані:

```
☐ 1. HTF тренд (1H+) визначений і ми торгуємо за ним
     (BOS вгору для лонгу, BOS вниз для шорту)

☐ 2. Ціна в Discount (для лонгу) або Premium (для шорту)
     відносно останнього значущого діапазону

☐ 3. Ціна на HTF POI (OB / FVG / Demand-Supply зона)

☐ 4. Ліквідність забрана (sweep EQH/EQL або inducement)

☐ 5. LTF підтвердження:
     - CHoCH на 1M/5M у бажаному напрямку
     - АБО SFP на POI
     - АБО Flip

☐ 6. SC (Sponsored Candle) або Wick у напрямку входу

☐ 7. Є чистий шлях до тейку (немає FTA між входом і ціллю)
     RR ≥ 2:1

☐ (додатково) Час — KillZone London/NY

☐ (додатково) Order Flow підтверджує:
     - Imbalance у напрямку входу
     - Delta spike
     - Absorption на POI
```

### Формула довіри (Confidence Score):

```python
def confidence_score(signal):
    score = 0
    checks = {
        'htf_trend_aligned': 2,      # ваги різні!
        'in_correct_zone': 2,         # discount/premium
        'at_poi': 2,
        'liquidity_swept': 1,
        'ltf_confirmation': 3,        # найважливіше
        'sc_or_wick': 1,
        'clear_path_to_tp': 2,
        'killzone_time': 1,
        'order_flow_aligned': 2,
    }
    for check, weight in checks.items():
        if signal[check]:
            score += weight
    # Max possible: 16
    return score / 16  # нормалізовано 0.0-1.0

# Правило: входимо тільки якщо score >= 0.65
```

---

## Резюме для бота

### Ієрархія прийняття рішень:

```
1. HTF контекст (1H, 4H):
   → Визначити фазу (Accumulation/Markup/Distribution/Markdown)
   → Знайти POI (OB, FVG, D/S зони)
   → Визначити напрямок торгівлі

2. MTF структура (15M, 5M):
   → Чекати поки ціна прийде до HTF POI
   → Визначити Premium/Discount відносно swing
   → Шукати перші ознаки реакції

3. LTF тригер (1M, 2M):
   → CHoCH / Flip / SFP у напрямку HTF тренду
   → VSR підтвердження
   → Order Flow (imbalance, delta spike) — додатковий фільтр

4. Виконання:
   → Вхід: тільки Confirmation Entry
   → Стоп: за POI (не за фіксованою відстанню)
   → Тейк: мінімум 2R, до наступного POI/ліквідності
   → Перевірити FTA на шляху

5. Після угоди:
   → Лог усіх умов які були виконані/не виконані
   → Аналіз помилок (чи був false signal, чи неправильний час, чи FTA не врахований)
```

### Заборонені дії:
- ❌ Торгувати без HTF контексту
- ❌ Входити на POI без LTF підтвердження
- ❌ Торгувати проти HTF тренду
- ❌ Ігнорувати FTA на шляху до тейку
- ❌ Входити під час важливих новин
- ❌ Торгувати в Азіатську сесію або Lunch Time
- ❌ Використовувати Risk Entry (тільки Confirmation Entry)
- ❌ Входити якщо RR < 2:1

---

*Кінець конспекту. Перегляд — після кожної торгової сесії.*
