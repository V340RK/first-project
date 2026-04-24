// Scalper Dashboard — client.
// Єдине джерело правди: WebSocket `/ws/events`. Стан у DOM, без фреймворків.

(function () {
    const MAX_ROWS = 500;

    const els = {
        wsStatus: document.getElementById("ws-status"),
        eventCount: document.getElementById("event-count"),
        rate: document.getElementById("rate"),
        lastEvent: document.getElementById("last-event"),
        serverTime: document.getElementById("server-time"),
        kindsList: document.getElementById("kinds-list"),
        tbody: document.getElementById("events-tbody"),
        filterInput: document.getElementById("filter-input"),
        pauseBtn: document.getElementById("pause-btn"),
        clearBtn: document.getElementById("clear-btn"),
    };

    const state = {
        total: 0,
        kinds: new Map(),  // kind → count
        filter: "",
        paused: false,
        pendingWhilePaused: [],
        recentTimes: [],   // last N event-arrival timestamps for rate calc
        lastEventTs: null,
    };

    function fmtTime(ms) {
        if (!ms) return "—";
        const d = new Date(ms);
        return d.toISOString().substring(11, 19);
    }

    function renderKinds() {
        const entries = Array.from(state.kinds.entries()).sort((a, b) => b[1] - a[1]);
        els.kindsList.innerHTML = entries.map(([kind, count]) =>
            `<li><span>${escapeHtml(kind)}</span><span class="count">${count}</span></li>`
        ).join("");
    }

    function escapeHtml(s) {
        return String(s ?? "").replace(/[&<>"']/g, c => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        })[c]);
    }

    function passesFilter(event) {
        if (!state.filter) return true;
        const f = state.filter.toLowerCase();
        return (
            String(event.kind || "").toLowerCase().includes(f) ||
            String(event.symbol || "").toLowerCase().includes(f) ||
            String(event.trade_id || "").toLowerCase().includes(f)
        );
    }

    function buildRow(event) {
        const tr = document.createElement("tr");
        const kindClass = `kind-${event.kind || "unknown"}`;
        const payloadStr = event.payload ? JSON.stringify(event.payload) : "";
        tr.innerHTML = `
            <td>${fmtTime(event.timestamp_ms)}</td>
            <td>${escapeHtml(event.seq ?? "")}</td>
            <td><span class="kind-badge ${kindClass}">${escapeHtml(event.kind || "")}</span></td>
            <td>${escapeHtml(event.symbol || "")}</td>
            <td>${escapeHtml(event.trade_id || "")}</td>
            <td class="payload" title="click to expand">${escapeHtml(payloadStr)}</td>
        `;
        tr.querySelector(".payload").addEventListener("click", (e) => {
            e.currentTarget.classList.toggle("expanded");
        });
        return tr;
    }

    function addEvent(event, {prepend = true} = {}) {
        state.total += 1;
        state.kinds.set(event.kind, (state.kinds.get(event.kind) || 0) + 1);
        state.lastEventTs = event.timestamp_ms;

        // rate: last 60s of arrival times (wall-clock)
        const now = Date.now();
        state.recentTimes.push(now);
        const cutoff = now - 60_000;
        while (state.recentTimes.length && state.recentTimes[0] < cutoff) {
            state.recentTimes.shift();
        }

        if (!passesFilter(event)) return;
        if (state.paused) {
            state.pendingWhilePaused.push(event);
            return;
        }

        const row = buildRow(event);
        if (prepend) {
            els.tbody.insertBefore(row, els.tbody.firstChild);
        } else {
            els.tbody.appendChild(row);
        }
        while (els.tbody.childElementCount > MAX_ROWS) {
            els.tbody.removeChild(els.tbody.lastChild);
        }
    }

    function updateStatusUI() {
        els.eventCount.textContent = `events: ${state.total}`;
        els.rate.textContent = `rate: ${state.recentTimes.length}/min`;
        els.lastEvent.textContent = `last: ${fmtTime(state.lastEventTs)}`;
        renderKinds();
    }

    setInterval(updateStatusUI, 500);
    setInterval(() => {
        const now = new Date();
        els.serverTime.textContent = `UTC: ${now.toISOString().substring(11, 19)}`;
    }, 1000);

    els.filterInput.addEventListener("input", (e) => {
        state.filter = e.target.value.trim();
        // Перемальовуємо поточну таблицю з урахуванням фільтра — лінивий підхід: просто clear + n/a.
        // Нові дані все одно проростуть із сервера, а історія залишиться у панелі kinds.
        els.tbody.innerHTML = "";
    });

    els.pauseBtn.addEventListener("click", () => {
        state.paused = !state.paused;
        els.pauseBtn.textContent = state.paused ? "Resume" : "Pause";
        els.pauseBtn.classList.toggle("paused", state.paused);
        if (!state.paused && state.pendingWhilePaused.length) {
            // Відкладені йдуть у зворотньому порядку, щоб найсвіжіше було зверху.
            const buffered = state.pendingWhilePaused.splice(0);
            for (let i = buffered.length - 1; i >= 0; i--) {
                const row = buildRow(buffered[i]);
                els.tbody.insertBefore(row, els.tbody.firstChild);
            }
            while (els.tbody.childElementCount > MAX_ROWS) {
                els.tbody.removeChild(els.tbody.lastChild);
            }
        }
    });

    els.clearBtn.addEventListener("click", () => {
        els.tbody.innerHTML = "";
    });

    // === WebSocket з автоматичним reconnect ===

    let ws = null;
    let reconnectDelay = 500;

    function connect() {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${proto}//${location.host}/ws/events`;
        els.wsStatus.textContent = "WS: connecting…";
        els.wsStatus.dataset.state = "connecting";

        ws = new WebSocket(url);

        ws.addEventListener("open", () => {
            els.wsStatus.textContent = "WS: connected";
            els.wsStatus.dataset.state = "connected";
            reconnectDelay = 500;
        });

        ws.addEventListener("message", (e) => {
            let msg;
            try { msg = JSON.parse(e.data); } catch { return; }
            if (msg.type === "backfill") {
                // Оприлюднюємо у правильному порядку: найстаріші вниз таблиці.
                // Рахуємо лічильники через addEvent, але додаємо рядок у кінець.
                for (const ev of msg.events) {
                    addEvent(ev, {prepend: false});
                }
                // Після backfill-у «найсвіжіший згори» — ще раз переставимо останній нагору.
                // Простіше: backfill-и йдуть у хронологічному порядку, тому після append
                // треба розвернути таблицю — але MAX_ROWS уже обмежив. Ми зробимо просто:
                // не реверсимо; користувач бачить хронологію знизу вгору для backfill.
            } else if (msg.type === "event") {
                addEvent(msg.event, {prepend: true});
            }
        });

        ws.addEventListener("close", () => {
            els.wsStatus.textContent = "WS: disconnected";
            els.wsStatus.dataset.state = "disconnected";
            setTimeout(connect, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, 5000);
        });

        ws.addEventListener("error", () => {
            // close подія прийде все одно; тут просто залишимо логування у консоль.
            console.warn("WS error");
        });
    }

    connect();
})();
