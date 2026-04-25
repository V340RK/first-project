(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const el = {
        addBox: $("add-pair-box"),
        pair: $("pair-input"),
        dropdown: $("symbol-dropdown"),
        symbolsCount: $("symbols-count"),
        slotsContainer: $("slots-container"),
        emptyState: $("empty-state"),
        slotTemplate: $("slot-template"),
        errorBanner: $("error-banner"),
        balanceAvailable: $("balance-available"),
        balanceWallet: $("balance-wallet"),
        balanceUpnl: $("balance-upnl"),
        balanceMeta: $("balance-meta"),
    };

    let lastBalance = null;   // {wallet_balance, available_balance, ...}

    const LS_KEY = "v340rk.slots";
    const state = {
        allSymbols: [],         // [{symbol, base, quote, tick_size, step_size}]
        slots: new Map(),       // symbol → {nodes, params}
        activeSuggestion: -1,
    };

    // === Form persistence (per-slot params, не статус — статус йде з backend) ===
    function saveSlots() {
        const data = [];
        state.slots.forEach((slot, sym) => {
            data.push({
                symbol: sym,
                leverage: slot.nodes.leverage.value,
                sizingMode: slot.nodes.sizingMode.value,
                sizingValue: slot.nodes.sizingValue.value,
                liqCap: slot.nodes.liqCap.value,
                slipTicks: slot.nodes.slipTicks.value,
                mode: slot.nodes.mode.value,
            });
        });
        try { localStorage.setItem(LS_KEY, JSON.stringify(data)); } catch (e) {}
    }

    function loadSlots() {
        try {
            const raw = localStorage.getItem(LS_KEY);
            if (!raw) return [];
            return JSON.parse(raw);
        } catch (e) { return []; }
    }

    // === Helpers ===
    function showError(msg) {
        el.errorBanner.textContent = msg;
        el.errorBanner.classList.remove("hidden");
        setTimeout(() => el.errorBanner.classList.add("hidden"), 8000);
    }

    function formatUptime(ms) {
        if (!ms || ms < 0) return "—";
        const s = Math.floor(ms / 1000);
        const hh = String(Math.floor(s / 3600)).padStart(2, "0");
        const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
        const ss = String(s % 60).padStart(2, "0");
        return `${hh}:${mm}:${ss}`;
    }

    function formatR(r) {
        if (typeof r !== "number") return "—";
        const sign = r > 0 ? "+" : "";
        return `${sign}${r.toFixed(2)} R`;
    }

    function formatUsd(usd) {
        if (typeof usd !== "number") return "—";
        const sign = usd > 0 ? "+" : "";
        return `${sign}${usd.toFixed(2)}`;
    }

    function pnlClass(val) {
        if (typeof val !== "number" || val === 0) return "";
        return val > 0 ? "positive" : "negative";
    }

    function formatLastEvent(ms) {
        if (!ms) return "—";
        const ago = Date.now() - ms;
        if (ago < 1000) return "щойно";
        if (ago < 60_000) return `${Math.floor(ago / 1000)}с тому`;
        if (ago < 3600_000) return `${Math.floor(ago / 60_000)}хв тому`;
        return new Date(ms).toLocaleTimeString();
    }

    // === Slot lifecycle ===
    function addSlot(symbol, prefill = null) {
        const sym = symbol.toUpperCase();
        if (state.slots.has(sym)) {
            showError(`Слот для ${sym} вже існує`);
            return null;
        }
        if (state.allSymbols.length > 0 && !state.allSymbols.find(s => s.symbol === sym)) {
            showError(`Пара "${sym}" не торгується на Binance Futures USDT-M`);
            return null;
        }

        const fragment = el.slotTemplate.content.cloneNode(true);
        const card = fragment.querySelector(".slot-card");
        card.dataset.symbol = sym;
        card.querySelector(".slot-symbol").textContent = sym;

        const nodes = {
            card,
            state: card.querySelector(".slot-state"),
            remove: card.querySelector(".slot-remove"),
            leverage: card.querySelector(".slot-leverage"),
            leverageVal: card.querySelector(".leverage-val"),
            sizingMode: card.querySelector(".slot-sizing-mode"),
            sizingValue: card.querySelector(".slot-sizing-value"),
            sizingHint: card.querySelector(".sizing-hint"),
            liqCap: card.querySelector(".slot-liq-cap"),
            slipTicks: card.querySelector(".slot-slip-ticks"),
            mode: card.querySelector(".slot-mode"),
            startBtn: card.querySelector(".slot-start"),
            stopBtn: card.querySelector(".slot-stop"),
            stat: {
                trades: card.querySelector(".stat-trades"),
                uptime: card.querySelector(".stat-uptime"),
                r: card.querySelector(".stat-r"),
                usd: card.querySelector(".stat-usd"),
                open: card.querySelector(".stat-open"),
                last: card.querySelector(".stat-last"),
            },
            book: {
                spread: card.querySelector(".spread-val"),
                asks: card.querySelector(".book-asks"),
                bids: card.querySelector(".book-bids"),
                meta: card.querySelector(".book-meta"),
            },
        };

        if (prefill) {
            if (prefill.leverage) nodes.leverage.value = prefill.leverage;
            if (prefill.sizingMode) nodes.sizingMode.value = prefill.sizingMode;
            if (prefill.sizingValue) nodes.sizingValue.value = prefill.sizingValue;
            if (prefill.liqCap) nodes.liqCap.value = prefill.liqCap;
            if (prefill.slipTicks) nodes.slipTicks.value = prefill.slipTicks;
            if (prefill.mode) nodes.mode.value = prefill.mode;
        }
        nodes.leverageVal.textContent = `${nodes.leverage.value}x`;

        const updateSizingHint = () => {
            if (nodes.sizingMode.value === "margin_pct") {
                nodes.sizingHint.textContent = `% від балансу як margin. Notional = margin × ${nodes.leverage.value}x плече.`;
            } else {
                nodes.sizingHint.textContent = "Скільки втратиш якщо стоп спрацює (R-based).";
            }
        };
        updateSizingHint();

        // Wire up events
        nodes.leverage.addEventListener("input", () => {
            nodes.leverageVal.textContent = `${nodes.leverage.value}x`;
            updateSizingHint();
            saveSlots();
        });
        nodes.sizingMode.addEventListener("change", () => {
            updateSizingHint();
            saveSlots();
        });
        ["change", "input"].forEach(ev => {
            [nodes.sizingValue, nodes.liqCap, nodes.slipTicks, nodes.mode].forEach(n => {
                n.addEventListener(ev, saveSlots);
            });
        });
        nodes.startBtn.addEventListener("click", () => startSlot(sym));
        nodes.stopBtn.addEventListener("click", () => stopSlot(sym));
        nodes.remove.addEventListener("click", () => removeSlot(sym));

        el.slotsContainer.appendChild(card);
        state.slots.set(sym, {nodes, running: false, bookTimer: null});

        // Стартуємо polling стакана
        const slot = state.slots.get(sym);
        const tick = () => fetchBook(sym).catch(() => {});
        tick();
        slot.bookTimer = setInterval(tick, 2000);

        el.emptyState.classList.add("hidden");
        saveSlots();
        return sym;
    }

    function removeSlot(sym) {
        const slot = state.slots.get(sym);
        if (!slot) return;
        if (slot.running) {
            showError(`Спочатку зупини бота для ${sym}`);
            return;
        }
        if (slot.bookTimer) clearInterval(slot.bookTimer);
        slot.nodes.card.remove();
        state.slots.delete(sym);
        if (state.slots.size === 0) el.emptyState.classList.remove("hidden");
        saveSlots();
    }

    // === Mini-book (orderbook widget) ===
    async function fetchBook(sym) {
        const slot = state.slots.get(sym);
        if (!slot) return;
        try {
            const resp = await fetch(`/api/orderbook/${sym}?depth=10`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            renderBook(slot, data);
        } catch (e) {
            slot.nodes.book.meta.textContent = `немає даних: ${e.message}`;
        }
    }

    function formatPrice(p) {
        if (p >= 1000) return p.toFixed(1);
        if (p >= 1) return p.toFixed(3);
        if (p >= 0.01) return p.toFixed(5);
        return p.toFixed(7);
    }

    function formatQty(q) {
        if (q >= 10000) return (q / 1000).toFixed(1) + "k";
        if (q >= 100) return q.toFixed(1);
        if (q >= 1) return q.toFixed(3);
        return q.toFixed(4);
    }

    function renderBook(slot, data) {
        const N = 5;   // показуємо top-5
        const asks = (data.asks || []).slice(0, N);
        const bids = (data.bids || []).slice(0, N);
        if (asks.length === 0 || bids.length === 0) {
            slot.nodes.book.meta.textContent = "пуста книжка";
            return;
        }
        const bestAsk = asks[0][0];
        const bestBid = bids[0][0];
        const spread = bestAsk - bestBid;
        const spreadPct = (spread / bestBid) * 100;
        slot.nodes.book.spread.textContent =
            `${formatPrice(spread)}  (${spreadPct.toFixed(3)}%)`;

        // Bar нормалізація — макс qty з обох сторін
        const allQtys = [...asks.map(a => a[1]), ...bids.map(b => b[1])];
        const maxQty = Math.max(...allQtys, 0.0001);

        const renderRow = (level, kind) => {
            const [price, qty] = level;
            const pct = (qty / maxQty) * 100;
            const bgClass = kind === "ask" ? "row-ask-bg" : "row-bid-bg";
            const priceCls = kind === "ask" ? "price-ask" : "price-bid";
            return `<tr>
                <td><div class="row-bg ${bgClass}" style="width:${pct.toFixed(1)}%"></div></td>
                <td class="num ${priceCls}">${formatPrice(price)}</td>
                <td class="num qty">${formatQty(qty)}</td>
            </tr>`;
        };

        // Asks: показуємо у зворотному порядку (від далеких до best)
        slot.nodes.book.asks.innerHTML = asks.slice().reverse()
            .map(l => renderRow(l, "ask")).join("");
        slot.nodes.book.bids.innerHTML = bids.map(l => renderRow(l, "bid")).join("");

        const ts = new Date(data.fetched_at_ms || Date.now());
        slot.nodes.book.meta.textContent =
            `top-${N}, оновлено ${ts.toLocaleTimeString()}`;
    }

    async function startSlot(sym) {
        const slot = state.slots.get(sym);
        if (!slot) return;
        const sizingMode = slot.nodes.sizingMode.value;
        const sizingVal = parseFloat(slot.nodes.sizingValue.value);

        const payload = {
            symbol: sym,
            leverage: parseInt(slot.nodes.leverage.value, 10),
            mode: slot.nodes.mode.value,
        };
        if (sizingMode === "margin_pct") {
            if (!(sizingVal > 0 && sizingVal <= 100)) {
                showError(`${sym}: % балансу має бути 0..100`); return;
            }
            payload.margin_per_trade_pct = sizingVal;
        } else {
            if (!(sizingVal > 0)) { showError(`${sym}: ризик USDT > 0`); return; }
            payload.risk_per_trade_usd = sizingVal;
        }
        const liqCap = parseFloat(slot.nodes.liqCap.value);
        if (liqCap > 0 && liqCap <= 100) payload.max_book_consumption_pct = liqCap;
        const slipTicks = parseInt(slot.nodes.slipTicks.value, 10);
        if (slipTicks > 0) payload.max_expected_slippage_ticks = slipTicks;
        if (lastBalance && lastBalance.available_balance <= 0) {
            showError(`${sym}: баланс акаунту = 0; пополни перед стартом`);
            return;
        }

        slot.nodes.startBtn.disabled = true;
        try {
            const resp = await fetch("/api/bot/start", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({detail: resp.statusText}));
                throw new Error(err.detail || `HTTP ${resp.status}`);
            }
        } catch (e) {
            showError(`${sym}: запуск не вдався — ${e.message}`);
            slot.nodes.startBtn.disabled = false;
        }
    }

    async function stopSlot(sym) {
        const slot = state.slots.get(sym);
        if (!slot) return;
        slot.nodes.stopBtn.disabled = true;
        try {
            const resp = await fetch("/api/bot/stop", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({symbol: sym}),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        } catch (e) {
            showError(`${sym}: зупинка не вдалася — ${e.message}`);
            slot.nodes.stopBtn.disabled = false;
        }
    }

    // === Render slot status from backend ===
    function renderSlot(sym, slotData) {
        const slot = state.slots.get(sym);
        if (!slot) return;
        const {bot, session} = slotData;
        const running = bot && bot.running;
        slot.running = running;

        slot.nodes.state.textContent = running ? "RUNNING" : "stopped";
        slot.nodes.state.className = "slot-state pill " + (running ? "pill-running" : "pill-stopped");
        slot.nodes.card.classList.toggle("is-running", running);
        slot.nodes.startBtn.disabled = running;
        slot.nodes.stopBtn.disabled = !running;
        slot.nodes.remove.disabled = running;
        // Заборона змінювати конфіг під час роботи (для прозорості)
        [slot.nodes.leverage, slot.nodes.sizingMode, slot.nodes.sizingValue,
         slot.nodes.liqCap, slot.nodes.slipTicks, slot.nodes.mode]
            .forEach(n => { n.disabled = running; });

        if (session) {
            slot.nodes.stat.trades.textContent = session.trades_closed ?? 0;
            slot.nodes.stat.uptime.textContent = running ? formatUptime(session.uptime_ms) : "—";
            slot.nodes.stat.r.textContent = formatR(session.realized_r);
            slot.nodes.stat.r.className = "stat-value stat-r " + pnlClass(session.realized_r);
            slot.nodes.stat.usd.textContent = formatUsd(session.realized_usd);
            slot.nodes.stat.usd.className = "stat-value stat-usd " + pnlClass(session.realized_usd);
            slot.nodes.stat.open.textContent = session.open_positions ?? 0;
            slot.nodes.stat.last.textContent = formatLastEvent(session.last_event_ms);
        }
    }

    function formatBalanceUsd(v) {
        if (typeof v !== "number") return "—";
        return v.toFixed(2);
    }

    async function fetchBalance() {
        try {
            const resp = await fetch("/api/account/balance");
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({detail: resp.statusText}));
                throw new Error(err.detail || `HTTP ${resp.status}`);
            }
            const b = await resp.json();
            lastBalance = b;
            el.balanceAvailable.textContent = `${formatBalanceUsd(b.available_balance)} ${b.quote_asset}`;
            el.balanceWallet.textContent = `${formatBalanceUsd(b.wallet_balance)} ${b.quote_asset}`;
            el.balanceUpnl.textContent = (b.total_unrealized_pnl >= 0 ? "+" : "") +
                `${formatBalanceUsd(b.total_unrealized_pnl)} ${b.quote_asset}`;
            el.balanceUpnl.className = "balance-value-small " +
                (b.total_unrealized_pnl > 0 ? "positive" :
                 b.total_unrealized_pnl < 0 ? "negative" : "");
            el.balanceMeta.textContent = `оновлено ${new Date(b.fetched_at_ms || Date.now()).toLocaleTimeString()}`;
            el.balanceMeta.classList.remove("error");
        } catch (e) {
            el.balanceAvailable.textContent = "недоступно";
            el.balanceMeta.textContent = `помилка: ${e.message}`;
            el.balanceMeta.classList.add("error");
            lastBalance = null;
        }
    }

    async function fetchStatus() {
        try {
            const resp = await fetch("/api/bot/status");
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const slots = data.slots || {};
            // Render тільки для тих slot-ів що є в UI; решту backend знає, але UI не показує
            state.slots.forEach((_, sym) => {
                const sd = slots[sym] || {bot: {running: false}, session: null};
                renderSlot(sym, sd);
            });
        } catch (e) {
            console.warn("status fetch failed:", e);
        }
    }

    // === Symbols typeahead (для додавання нового слота) ===
    function filterSuggestions(query) {
        const q = query.toUpperCase();
        const occupied = state.slots;
        const available = state.allSymbols.filter(s => !occupied.has(s.symbol));
        if (!q) {
            const priority = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"];
            const top = priority.map(p => available.find(s => s.symbol === p)).filter(Boolean);
            const rest = available.filter(s => !priority.includes(s.symbol));
            return [...top, ...rest].slice(0, 20);
        }
        const startsWith = available.filter(s => s.symbol.startsWith(q) || s.base.startsWith(q));
        const contains = available.filter(s =>
            !startsWith.includes(s) && (s.symbol.includes(q) || s.base.includes(q))
        );
        return [...startsWith, ...contains].slice(0, 20);
    }

    function renderDropdown(query) {
        const items = filterSuggestions(query);
        el.dropdown.innerHTML = "";
        state.activeSuggestion = -1;
        if (state.allSymbols.length === 0) {
            el.dropdown.innerHTML = '<div class="dropdown-empty">Завантаження списку пар…</div>';
        } else if (items.length === 0) {
            el.dropdown.innerHTML = '<div class="dropdown-empty">Нічого не знайдено</div>';
        } else {
            items.forEach((s) => {
                const div = document.createElement("div");
                div.className = "dropdown-item";
                div.dataset.symbol = s.symbol;
                div.innerHTML = `
                    <span class="sym">${s.symbol}</span>
                    <span class="meta">${s.base}/${s.quote} · tick ${s.tick_size}</span>
                `;
                div.addEventListener("mousedown", (e) => {
                    e.preventDefault();
                    if (addSlot(s.symbol)) {
                        el.pair.value = "";
                        hideDropdown();
                        el.pair.focus();
                    }
                });
                el.dropdown.appendChild(div);
            });
        }
        showDropdown();
    }

    function showDropdown() { el.dropdown.classList.remove("hidden"); }
    function hideDropdown() { el.dropdown.classList.add("hidden"); }

    function moveActive(delta) {
        const items = el.dropdown.querySelectorAll(".dropdown-item");
        if (items.length === 0) return;
        state.activeSuggestion = (state.activeSuggestion + delta + items.length) % items.length;
        items.forEach((it, i) => it.classList.toggle("active", i === state.activeSuggestion));
        items[state.activeSuggestion].scrollIntoView({block: "nearest"});
    }

    function commitActive() {
        const items = el.dropdown.querySelectorAll(".dropdown-item");
        const picked = items[state.activeSuggestion] || items[0];
        if (!picked) return false;
        if (addSlot(picked.dataset.symbol)) {
            el.pair.value = "";
            hideDropdown();
            return true;
        }
        return false;
    }

    // === Load symbols ===
    async function loadSymbols() {
        try {
            const resp = await fetch("/api/symbols");
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            state.allSymbols = await resp.json();
            el.symbolsCount.textContent = state.allSymbols.length;
        } catch (e) {
            el.symbolsCount.textContent = "недоступно";
            showError(`Не вдалося завантажити список пар: ${e.message}`);
        }
    }

    // === Wire up ===
    el.pair.addEventListener("focus", () => renderDropdown(el.pair.value));
    el.pair.addEventListener("input", () => renderDropdown(el.pair.value));
    el.pair.addEventListener("blur", () => setTimeout(hideDropdown, 150));
    el.pair.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") { e.preventDefault(); moveActive(1); }
        else if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); }
        else if (e.key === "Enter") {
            e.preventDefault();
            if (!commitActive() && el.pair.value.trim()) {
                if (addSlot(el.pair.value)) {
                    el.pair.value = "";
                    hideDropdown();
                }
            }
        }
        else if (e.key === "Escape") { hideDropdown(); }
    });
    el.addBox.addEventListener("click", (e) => {
        if (e.target === el.addBox) el.pair.focus();
    });

    // Initial boot: load symbols, then restore slots з localStorage
    loadSymbols().then(() => {
        const saved = loadSlots();
        let restored = 0;
        for (const item of saved) {
            if (state.allSymbols.find(s => s.symbol === item.symbol)) {
                if (addSlot(item.symbol, item)) restored++;
            }
        }
        if (saved.length > 0 && restored < saved.length) {
            const lost = saved.filter(it => !state.slots.has(it.symbol)).map(it => it.symbol);
            showError(`Прибрано слоти невалідних пар: ${lost.join(", ")}`);
        }
    });
    fetchStatus();
    setInterval(fetchStatus, 1000);
    fetchBalance();
    setInterval(fetchBalance, 3000);    // баланс не змінюється так часто
})();
