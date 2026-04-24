(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const el = {
        chipsBox: $("chips-box"),
        chips: $("chips"),
        pair: $("pair-input"),
        dropdown: $("symbol-dropdown"),
        symbolsCount: $("symbols-count"),
        leverage: $("leverage"),
        leverageVal: $("leverage-val"),
        risk: $("risk"),
        equity: $("equity"),
        mode: $("mode"),
        startBtn: $("start-btn"),
        stopBtn: $("stop-btn"),
        state: $("bot-state"),
        errorBanner: $("error-banner"),
        stat: {
            trades: $("stat-trades"),
            uptime: $("stat-uptime"),
            r: $("stat-r"),
            usd: $("stat-usd"),
            open: $("stat-open"),
            last: $("stat-last"),
        },
    };

    const LS_KEY = "v340rk.form";
    const state = {
        allSymbols: [],           // [{symbol, base, quote, tick_size, step_size}]
        selected: new Set(),      // Set<string>
        activeSuggestion: -1,     // for keyboard nav
    };

    // === Form persistence ===
    function saveForm() {
        const data = {
            symbols: Array.from(state.selected),
            leverage: el.leverage.value,
            risk: el.risk.value,
            equity: el.equity.value,
            mode: el.mode.value,
        };
        try { localStorage.setItem(LS_KEY, JSON.stringify(data)); } catch (e) {}
    }

    function loadForm() {
        try {
            const raw = localStorage.getItem(LS_KEY);
            if (!raw) return;
            const d = JSON.parse(raw);
            if (Array.isArray(d.symbols)) d.symbols.forEach(s => addChip(s, true));
            if (d.leverage) el.leverage.value = d.leverage;
            if (d.risk) el.risk.value = d.risk;
            if (d.equity) el.equity.value = d.equity;
            if (d.mode) el.mode.value = d.mode;
        } catch (e) {}
    }

    // === Chips ===
    function renderChips() {
        el.chips.innerHTML = "";
        for (const sym of state.selected) {
            const chip = document.createElement("span");
            chip.className = "chip";
            chip.innerHTML = `<span>${sym}</span>`;
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "chip-remove";
            btn.textContent = "×";
            btn.title = `Видалити ${sym}`;
            btn.addEventListener("click", () => {
                state.selected.delete(sym);
                renderChips();
                saveForm();
            });
            chip.appendChild(btn);
            el.chips.appendChild(chip);
        }
    }

    function addChip(symRaw, skipSave = false) {
        const sym = symRaw.toUpperCase().trim();
        if (!sym) return false;
        // Валідація: має бути в allSymbols (коли список завантажено)
        if (state.allSymbols.length > 0 && !state.allSymbols.find(s => s.symbol === sym)) {
            showError(`Пара "${sym}" не торгується на Binance Futures USDT-M`);
            return false;
        }
        state.selected.add(sym);
        renderChips();
        if (!skipSave) saveForm();
        return true;
    }

    // === Dropdown / typeahead ===
    function filterSuggestions(query) {
        const q = query.toUpperCase();
        const available = state.allSymbols.filter(s => !state.selected.has(s.symbol));
        if (!q) {
            // show top по популярності (BTC, ETH, SOL першими, решта alphabetical)
            const priorityOrder = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"];
            const priority = priorityOrder.map(p => available.find(s => s.symbol === p)).filter(Boolean);
            const rest = available.filter(s => !priorityOrder.includes(s.symbol));
            return [...priority, ...rest].slice(0, 20);
        }
        // спочатку ті що починаються з запиту, потім ті що містять
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
            items.forEach((s, i) => {
                const div = document.createElement("div");
                div.className = "dropdown-item";
                div.dataset.symbol = s.symbol;
                div.innerHTML = `
                    <span class="sym">${s.symbol}</span>
                    <span class="meta">${s.base}/${s.quote} · tick ${s.tick_size}</span>
                `;
                div.addEventListener("mousedown", (e) => {
                    e.preventDefault();  // щоб не спрацював blur на input
                    addChip(s.symbol);
                    el.pair.value = "";
                    hideDropdown();
                    el.pair.focus();
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
        addChip(picked.dataset.symbol);
        el.pair.value = "";
        hideDropdown();
        return true;
    }

    // === Load symbols from backend ===
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

    // === Formatters & helpers ===
    function showError(msg) {
        el.errorBanner.textContent = msg;
        el.errorBanner.classList.remove("hidden");
        setTimeout(() => el.errorBanner.classList.add("hidden"), 8000);
    }

    function updateLeverageLabel() {
        el.leverageVal.textContent = `${el.leverage.value}x`;
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

    // === Render bot state / stats ===
    function renderBotState(running) {
        if (running) {
            el.state.textContent = "RUNNING";
            el.state.className = "pill pill-running";
            el.startBtn.disabled = true;
            el.stopBtn.disabled = false;
        } else {
            el.state.textContent = "stopped";
            el.state.className = "pill pill-stopped";
            el.startBtn.disabled = false;
            el.stopBtn.disabled = true;
        }
    }

    function renderStats(session) {
        el.stat.trades.textContent = session.trades_closed ?? 0;
        el.stat.uptime.textContent = formatUptime(session.uptime_ms);
        el.stat.r.textContent = formatR(session.realized_r);
        el.stat.r.className = "stat-value " + pnlClass(session.realized_r);
        el.stat.usd.textContent = formatUsd(session.realized_usd);
        el.stat.usd.className = "stat-value " + pnlClass(session.realized_usd);
        el.stat.open.textContent = session.open_positions ?? 0;
        el.stat.last.textContent = formatLastEvent(session.last_event_ms);
    }

    async function fetchStatus() {
        try {
            const resp = await fetch("/api/bot/status");
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            renderBotState(data.bot.running);
            renderStats(data.session);
        } catch (e) {
            console.warn("status fetch failed:", e);
        }
    }

    // === Start/Stop ===
    async function startBot() {
        const symbols = Array.from(state.selected);
        if (symbols.length === 0) {
            showError("Обери хоча б одну пару");
            return;
        }
        const payload = {
            symbols,
            leverage: parseInt(el.leverage.value, 10),
            risk_per_trade_usd: parseFloat(el.risk.value),
            equity_usd: parseFloat(el.equity.value),
            mode: el.mode.value,
        };
        if (!(payload.risk_per_trade_usd > 0)) { showError("Ризик > 0"); return; }
        if (!(payload.equity_usd > 0)) { showError("Баланс > 0"); return; }

        el.startBtn.disabled = true;
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
            saveForm();
        } catch (e) {
            showError(`Запуск не вдався: ${e.message}`);
            el.startBtn.disabled = false;
        }
    }

    async function stopBot() {
        el.stopBtn.disabled = true;
        try {
            const resp = await fetch("/api/bot/stop", {method: "POST"});
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        } catch (e) {
            showError(`Зупинка не вдалася: ${e.message}`);
            el.stopBtn.disabled = false;
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
            if (!commitActive()) {
                // якщо нічого не вибрано але введено текст напряму (наприклад "BTCUSDT")
                if (el.pair.value.trim()) {
                    if (addChip(el.pair.value)) el.pair.value = "";
                    hideDropdown();
                }
            }
        }
        else if (e.key === "Escape") { hideDropdown(); }
        else if (e.key === "Backspace" && !el.pair.value && state.selected.size > 0) {
            const last = Array.from(state.selected).pop();
            state.selected.delete(last);
            renderChips();
            saveForm();
        }
    });
    el.chipsBox.addEventListener("click", (e) => {
        if (e.target === el.chipsBox || e.target === el.chips) el.pair.focus();
    });
    el.leverage.addEventListener("input", updateLeverageLabel);
    el.startBtn.addEventListener("click", startBot);
    el.stopBtn.addEventListener("click", stopBot);
    ["input", "change"].forEach(ev => {
        [el.leverage, el.risk, el.equity, el.mode].forEach(node => {
            node.addEventListener(ev, saveForm);
        });
    });

    // Initial boot
    updateLeverageLabel();
    loadForm();   // chips з localStorage (додадуться тільки якщо валідні після loadSymbols)
    loadSymbols().then(() => {
        // після того як список прийшов — перевіряємо що chips з localStorage валідні
        const toRemove = [];
        for (const sym of state.selected) {
            if (!state.allSymbols.find(s => s.symbol === sym)) toRemove.push(sym);
        }
        toRemove.forEach(sym => state.selected.delete(sym));
        if (toRemove.length > 0) {
            renderChips();
            showError(`Прибрано невалідні пари: ${toRemove.join(", ")}`);
        }
    });
    fetchStatus();
    setInterval(fetchStatus, 1000);
})();
