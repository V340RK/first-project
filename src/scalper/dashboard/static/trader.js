(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const el = {
        pair: $("pair-input"),
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

    function saveForm() {
        const data = {
            pair: el.pair.value,
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
            if (d.pair) el.pair.value = d.pair;
            if (d.leverage) el.leverage.value = d.leverage;
            if (d.risk) el.risk.value = d.risk;
            if (d.equity) el.equity.value = d.equity;
            if (d.mode) el.mode.value = d.mode;
        } catch (e) {}
    }

    function updateLeverageLabel() {
        el.leverageVal.textContent = `${el.leverage.value}x`;
    }

    function parsePairs(raw) {
        return raw
            .split(/[,\s]+/)
            .map(s => s.trim().toUpperCase())
            .filter(Boolean);
    }

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

    async function startBot() {
        const pairs = parsePairs(el.pair.value);
        if (pairs.length === 0) {
            showError("Вкажи хоча б одну пару (напр. BTCUSDT)");
            return;
        }
        const payload = {
            symbols: pairs,
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

    el.leverage.addEventListener("input", updateLeverageLabel);
    el.startBtn.addEventListener("click", startBot);
    el.stopBtn.addEventListener("click", stopBot);
    ["input", "change"].forEach(ev => {
        [el.pair, el.leverage, el.risk, el.equity, el.mode].forEach(node => {
            node.addEventListener(ev, saveForm);
        });
    });

    loadForm();
    updateLeverageLabel();
    fetchStatus();
    setInterval(fetchStatus, 1000);
})();
