"""Embedded dashboard template for the sports reservation daemon."""

from __future__ import annotations


def create_dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>体育场馆预约守护进程</title>
  <style>
:root {
    --bg: #0a0a0f;
    --card-bg: #1a1a1a;
    --text: #eee;
    --sub-text: #888;
    --accent: #3fb950;
    --warn: #d29922;
    --error: #f85149;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    padding: 2rem 1rem;
    line-height: 1.5;
}
.wrap { max-width: 540px; margin: 0 auto; }

/* Header & Live Indicator */
header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2rem; }
h1 { font-size: 1.25rem; font-weight: 600; letter-spacing: -0.5px; }
.status-dot {
    font-size: 0.75rem; color: var(--accent); display: flex; align-items: center; gap: 6px;
    background: rgba(63, 185, 80, 0.1); padding: 4px 10px; border-radius: 20px;
}
.pulse { display: inline-block; width: 6px; height: 6px; background: currentColor; border-radius: 50%; animation: blink 2s infinite; }

/* Sections */
h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; color: var(--sub-text); margin: 0 0 1rem 0; font-weight: 600; }
.section { margin-bottom: 2.5rem; }

/* Hero Status Card */
.hero {
    background: var(--card-bg);
    padding: 1.5rem;
    border-radius: 12px;
    text-align: center;
}
.hero-status { font-size: 1.1rem; margin-bottom: 0.25rem; font-weight: bold; }
.hero-sub { color: var(--sub-text); font-size: 0.8rem; font-family: monospace; }

/* Badges row */
.badges { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; margin-top: 0.75rem; }
.badge {
    font-size: 0.7rem; padding: 3px 8px; border-radius: 12px;
    background: rgba(63, 185, 80, 0.1); color: var(--accent); font-family: monospace;
}

/* Form */
.form-section { margin-bottom: 2.5rem; }
label { display: block; font-size: 0.8rem; font-weight: 600; color: var(--sub-text); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
input, select {
    width: 100%; border: 1px solid #333; border-radius: 8px; padding: 10px;
    background: var(--card-bg); color: var(--text); font: inherit; font-size: 0.9rem;
    outline: none; -webkit-appearance: none; appearance: none;
}
select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23888' d='M2 4l4 4 4-4'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 10px center; padding-right: 28px;
}
input[type="checkbox"] {
    width: auto;
    accent-color: var(--accent);
    -webkit-appearance: auto;
    appearance: auto;
    padding: 0;
    border: none;
}
input:focus, select:focus { border-color: var(--accent); }
.field-row { margin-bottom: 0.75rem; }
.field-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.inline-row { display: flex; gap: 8px; align-items: end; }
.inline-row input { flex: 1; }

/* Buttons */
button {
    border: 0; border-radius: 8px; padding: 8px 14px; font: inherit; font-size: 0.8rem;
    font-weight: 600; cursor: pointer; transition: opacity 0.15s;
}
button:hover { opacity: 0.85; }
.btn-primary { background: var(--accent); color: #000; }
.btn-ghost { background: transparent; color: var(--sub-text); border: 1px solid #333; }
.btn-warn { background: rgba(210, 153, 34, 0.15); color: var(--warn); border: 1px solid rgba(210, 153, 34, 0.3); }
.btn-danger { background: rgba(248, 81, 73, 0.12); color: var(--error); border: 1px solid rgba(248, 81, 73, 0.25); }
.btn-run { background: rgba(63, 185, 80, 0.12); color: var(--accent); border: 1px solid rgba(63, 185, 80, 0.25); }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 0.75rem; }

/* Time-slot checkboxes */
.slot-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.slot-grid label {
    display: inline-flex; align-items: center; gap: 5px; font-size: 0.8rem; font-weight: 400;
    color: var(--text); text-transform: none; letter-spacing: 0; margin: 0; padding: 5px 10px;
    border-radius: 6px; background: rgba(255,255,255,0.04); cursor: pointer; white-space: nowrap;
}
.slot-grid label:hover { background: rgba(255,255,255,0.08); }

/* Info box (availability) */
.info-box {
    border-radius: 8px; padding: 12px; margin-top: 0.75rem;
    background: rgba(255,255,255,0.04); color: var(--sub-text); font-size: 0.85rem; line-height: 1.6;
}
.hidden { display: none !important; }

/* Jobs List */
.job {
    background: var(--card-bg); border-radius: 10px; padding: 1rem; margin-bottom: 0.75rem;
}
.job-head { display: flex; justify-content: space-between; align-items: start; gap: 8px; }
.job-name { font-size: 1rem; font-weight: 600; margin: 0; }
.job-meta { color: var(--sub-text); font-size: 0.8rem; margin-top: 4px; line-height: 1.6; }
.status-badge {
    font-size: 0.65rem; padding: 3px 8px; border-radius: 12px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; flex-shrink: 0;
}
.st-success, .st-idle { background: rgba(63, 185, 80, 0.12); color: var(--accent); }
.st-waiting_window, .st-no_slots, .st-preview_ready { background: rgba(210, 153, 34, 0.12); color: var(--warn); }
.st-submit_failed, .st-expired, .st-captcha_required { background: rgba(248, 81, 73, 0.12); color: var(--error); }
.job-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 0.75rem; }
.job-actions button { font-size: 0.75rem; padding: 5px 10px; }

/* History List */
.list { display: flex; flex-direction: column; gap: 0.75rem; }
.hist-item { display: flex; justify-content: space-between; align-items: baseline; font-size: 0.85rem; gap: 8px; }
.hist-msg { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hist-time { color: var(--sub-text); font-size: 0.7rem; font-family: monospace; flex-shrink: 0; }
.dot { margin-right: 6px; font-weight: bold; }
.st-ok { color: var(--accent); }
.st-fail { color: var(--error); }

/* Availability */
.avail-day { background: rgba(255,255,255,0.03); border-radius: 8px; padding: 10px; margin-bottom: 8px; }
.avail-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; font-size: 0.85rem; }
.avail-title { font-weight: 600; }
.avail-slots { display: flex; flex-wrap: wrap; gap: 6px; }
.avail-pill {
    font-size: 0.75rem; padding: 3px 8px; border-radius: 10px;
    background: rgba(63, 185, 80, 0.08); color: var(--accent);
}

/* Footer */
.meta-footer {
    border-top: 1px solid #222; padding-top: 1.5rem; margin-top: 1rem;
    font-size: 0.7rem; color: var(--sub-text); display: flex; justify-content: space-between;
}
.empty { color: var(--sub-text); font-style: italic; font-size: 0.85rem; }

@keyframes blink { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
  </style>
</head>
<body>
<div class="wrap">
    <header>
        <h1>交我约</h1>
        <div class="status-dot"><span class="pulse"></span> 守护进程</div>
    </header>

    <!-- Status -->
    <div class="section">
        <h2>状态</h2>
        <div class="hero">
            <div class="hero-status" id="heroStatus">加载中…</div>
            <div class="hero-sub" id="heroSub"></div>
            <div class="badges">
                <span class="badge" id="authMode">…</span>
                <span class="badge" id="nextRun">…</span>
            </div>
        </div>
    </div>

    <!-- Create Job -->
    <div class="form-section">
        <h2>新建任务</h2>
        <div class="field-row">
            <label for="jobName">任务名称</label>
            <input id="jobName" placeholder="例如：周一乒乓球">
        </div>
        <div class="field-pair">
            <div class="field-row">
                <label for="jobType">模式</label>
                <select id="jobType" onchange="toggleJobMode()">
                    <option value="target_date">指定日期</option>
                    <option value="cron">定时监控</option>
                </select>
            </div>
            <div class="field-row" id="targetDateRow">
                <label for="targetDate">日期</label>
                <input id="targetDate" type="date">
            </div>
        </div>
        <div class="field-row" id="runOnDateRow">
            <label for="runOnDate">运行日期（当天中午）</label>
            <input id="runOnDate" type="date">
        </div>
        <div class="field-row">
            <label for="venueSearch">场馆搜索</label>
            <div class="inline-row">
                <input id="venueSearch" placeholder="搜索场馆名称">
                <button type="button" class="btn-ghost" onclick="searchVenues()">搜索</button>
            </div>
        </div>
        <div class="field-row">
            <label for="venueSelect">场馆</label>
            <select id="venueSelect" onchange="loadVenueDetail()">
                <option value="">选择场馆</option>
            </select>
        </div>
        <div class="field-pair">
            <div class="field-row">
                <label for="motionSelect">运动类型</label>
                <select id="motionSelect" onchange="loadAvailability()">
                    <option value="">选择运动类型</option>
                </select>
            </div>
            <div class="field-row" id="retryWindowRow">
                <label for="retryWindow">重试窗口 (秒)</label>
                <input id="retryWindow" type="number" min="10" value="180">
            </div>
        </div>
        <div class="field-pair hidden" id="cronWindowRows">
            <div class="field-row">
                <label for="windowStartDays">窗口开始 (距今天数)</label>
                <input id="windowStartDays" type="number" min="0" value="0">
            </div>
            <div class="field-row">
                <label for="windowEndDays">窗口结束 (距今天数)</label>
                <input id="windowEndDays" type="number" min="0" value="7">
            </div>
        </div>
        <div class="field-pair hidden" id="cronTimingRows">
            <div class="field-row">
                <label for="cronIntervalMinutes">检查间隔 (分钟)</label>
                <input id="cronIntervalMinutes" type="number" min="1" value="10">
            </div>
            <div class="field-row">
                <label for="redeemDeadlineHours">下单截止 (提前小时数)</label>
                <input id="redeemDeadlineHours" type="number" min="0" value="2">
            </div>
        </div>
        <div class="field-pair">
            <div class="field-row">
                <label for="preferredFields">偏好场地</label>
                <input id="preferredFields" placeholder="按优先级填写，如：场地1,场地2,场地3">
            </div>
            <div class="field-row" id="retryIntervalRow">
                <label for="retryInterval">重试间隔 (秒)</label>
                <input id="retryInterval" type="number" min="0.2" step="0.1" value="0.2">
            </div>
        </div>
        <div class="field-row">
            <label for="slotSelectionMode">时间段策略</label>
            <select id="slotSelectionMode">
                <option value="first_available">按优先级抢一个场地时段</option>
                <option value="all_required">必须同时满足所有已选时间段</option>
            </select>
        </div>
        <div class="field-row">
            <label>时间段</label>
            <div class="slot-grid" id="timeSlotGrid"></div>
        </div>
        <div class="btn-row">
            <button type="button" class="btn-primary" onclick="createJob()">保存任务</button>
            <button type="button" class="btn-ghost" onclick="refreshStatus()">刷新</button>
        </div>
        <div id="modeHelp" class="info-box">指定日期模式默认会在下一个到来的中午运行，你也可以手动指定具体哪一天中午执行。时间段策略选择“按优先级抢一个场地时段”时，会按你勾选时间段的先后顺序，以及偏好场地的填写顺序，从同一次可用性检查结果里挑出第一个可下单组合，只提交一个场地，适合“每天同项目只能下一单”的规则。定时监控模式将每隔几分钟扫描配置的日期窗口内的新释放场地，并在下单截止前预订。</div>
        <div id="availabilityBox" class="info-box">搜索场馆以查看可用性。</div>
    </div>

    <!-- Jobs -->
    <div class="section">
        <h2>任务列表</h2>
        <div id="jobs"></div>
    </div>

    <!-- History -->
    <div class="section">
        <h2>最近活动</h2>
        <div id="history" class="list"></div>
    </div>

    <div class="meta-footer">
        <div>体育场馆预约守护进程</div>
        <div>每15秒自动刷新</div>
    </div>
</div>

<script>
const TIME_SLOTS = {{ time_slots | safe }};
let cachedVenues = [];
let timeSlotPriority = [];

function esc(v) {
    return String(v ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function selectedTimeSlots() {
    const checked = new Set(
        [...document.querySelectorAll('input[name="timeSlot"]:checked')].map(i => i.value)
    );
    timeSlotPriority = timeSlotPriority.filter(value => checked.has(value));
    return [...timeSlotPriority];
}

function updateTimeSlotPriority(slot, checked) {
    timeSlotPriority = timeSlotPriority.filter(value => value !== slot);
    if (checked) timeSlotPriority.push(slot);
}

function formatLocalDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function defaultRunOnDateValue() {
    const now = new Date();
    const noonPassed =
        now.getHours() > 12 ||
        (now.getHours() === 12 && (now.getMinutes() > 0 || now.getSeconds() > 0 || now.getMilliseconds() > 0));
    const runDate = new Date(now);
    if (noonPassed) runDate.setDate(runDate.getDate() + 1);
    return formatLocalDate(runDate);
}

function resetRunOnDateDefault(force = false) {
    const input = document.getElementById("runOnDate");
    if (force || !input.value) {
        input.value = defaultRunOnDateValue();
    }
}

function toggleJobMode() {
    const mode = document.getElementById("jobType").value;
    const isCron = mode === "cron";
    document.getElementById("targetDateRow").classList.toggle("hidden", isCron);
    document.getElementById("runOnDateRow").classList.toggle("hidden", isCron);
    document.getElementById("retryWindowRow").classList.toggle("hidden", isCron);
    document.getElementById("retryIntervalRow").classList.toggle("hidden", isCron);
    document.getElementById("cronWindowRows").classList.toggle("hidden", !isCron);
    document.getElementById("cronTimingRows").classList.toggle("hidden", !isCron);
    if (!isCron) resetRunOnDateDefault();
}

function renderTimeSlots() {
    document.getElementById("timeSlotGrid").innerHTML = TIME_SLOTS.map(s =>
        `<label><input type="checkbox" name="timeSlot" value="${s}" onchange="updateTimeSlotPriority('${s}', this.checked)"> ${s}</label>`
    ).join("");
}

async function api(path, opts = {}) {
    const r = await fetch(path, { headers: {"Content-Type":"application/json"}, ...opts });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || d.message || "Request failed");
    return d;
}

async function searchVenues() {
    const q = document.getElementById("venueSearch").value.trim();
    const d = await api(`/api/catalog/venues?search=${encodeURIComponent(q)}`);
    cachedVenues = d.venues || [];
    document.getElementById("venueSelect").innerHTML =
        `<option value="">选择场馆</option>` +
        cachedVenues.map(v => `<option value="${v.venueId}">${esc(v.venueName)} · ${esc(v.campusName||"")}</option>`).join("");
}

async function loadVenueDetail() {
    const vid = document.getElementById("venueSelect").value;
    const ms = document.getElementById("motionSelect");
    ms.innerHTML = `<option value="">选择运动类型</option>`;
    if (!vid) return;
    const d = await api(`/api/catalog/venues/${vid}`);
    const v = d.venue;
    ms.innerHTML += v.motion_types.map(m => `<option value="${m.name}">${esc(m.name)}</option>`).join("");
    document.getElementById("availabilityBox").innerHTML =
        `<strong>${esc(v.venue_name)}</strong><br>${esc(v.campus_name)} · ${esc(v.open_time)}<br>${esc(v.venue_mobile)}`;
}

async function loadAvailability() {
    const vid = document.getElementById("venueSelect").value;
    const mot = document.getElementById("motionSelect").value;
    if (!vid || !mot) return;
    const d = await api(`/api/catalog/venues/${vid}/availability?motion=${encodeURIComponent(mot)}`);
    const html = d.availability.map(item => {
        const slots = item.selectable_slots || [];
        const pills = slots.length
            ? `<div class="avail-slots">${slots.map(s =>
                `<span class="avail-pill">${esc(s.field_name)} · ${esc(s.time_slot)} · ¥${esc(s.price)}</span>`
              ).join("")}</div>`
            : `<div class="empty">没有可选时间段。</div>`;
        return `<div class="avail-day">
            <div class="avail-head"><span class="avail-title">${esc(item.date)} · ${esc(item.view_str)}</span><span style="color:var(--sub-text);font-size:0.75rem">${item.selectable_count} 个空余</span></div>
            ${pills}</div>`;
    }).join("");
    document.getElementById("availabilityBox").innerHTML = html || `<div class="empty">没有可见日期。</div>`;
}

async function createJob() {
    const sel = document.getElementById("venueSelect");
    const jobType = document.getElementById("jobType").value;
    const payload = {
        job_type: jobType,
        name: document.getElementById("jobName").value.trim(),
        venue_id: sel.value,
        venue_name: sel.selectedOptions[0]?.textContent?.split(" · ")[0] || "",
        motion: document.getElementById("motionSelect").value,
        target_date: jobType === "target_date" ? document.getElementById("targetDate").value : "",
        run_on_date: jobType === "target_date" ? document.getElementById("runOnDate").value : "",
        slot_selection_mode: document.getElementById("slotSelectionMode").value,
        preferred_fields: document.getElementById("preferredFields").value,
        time_slots: selectedTimeSlots(),
        retry_window_seconds: Number(document.getElementById("retryWindow").value || 180),
        retry_interval_seconds: Number(document.getElementById("retryInterval").value || 0.2),
        cron_interval_minutes: Number(document.getElementById("cronIntervalMinutes").value || 10),
        window_start_days: Number(document.getElementById("windowStartDays").value || 0),
        window_end_days: Number(document.getElementById("windowEndDays").value || 7),
        redeem_deadline_hours: Number(document.getElementById("redeemDeadlineHours").value || 2),
        enabled: true,
    };
    await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    document.getElementById("jobName").value = "";
    document.getElementById("preferredFields").value = "";
    resetRunOnDateDefault(true);
    document.querySelectorAll('input[name="timeSlot"]').forEach(i => { i.checked = false; });
    timeSlotPriority = [];
    await refreshStatus();
}

function slotSelectionModeLabel(mode) {
    if (mode === "first_available") return "按优先级抢一个";
    return "全部时间段都要满足";
}

async function toggleJob(id, en) {
    await api(`/api/jobs/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: en }) });
    await refreshStatus();
}

async function deleteJob(id) {
    await api(`/api/jobs/${id}`, { method: "DELETE" });
    await refreshStatus();
}

async function runJob(id, dry) {
    const d = await api(`/api/jobs/${id}/run`, { method: "POST", body: JSON.stringify({ dry_run: dry }) });
    alert(d.result?.message || d.result?.order_id || "完成。");
    await refreshStatus();
}

function statusClass(s) {
    if (["success","idle"].includes(s)) return "st-success";
    if (["waiting_window","waiting_schedule","no_slots","preview_ready"].includes(s)) return "st-waiting_window";
    return "st-submit_failed";
}

function renderJobs(jobs) {
    const el = document.getElementById("jobs");
    if (!jobs.length) { el.innerHTML = `<div class="empty">暂无任务。</div>`; return; }
    el.innerHTML = jobs.map(j => `
        <div class="job">
            <div class="job-head">
                <div>
                    <div class="job-name">${esc(j.name)}</div>
                    <div class="job-meta">
                        ${esc(j.venue_name)} · ${esc(j.motion)}<br>
                        ${j.job_type === "cron"
                            ? `定时检查 每 ${esc(j.cron_interval_minutes)} 分钟 · 天数 +${esc(j.window_start_days)} 至 +${esc(j.window_end_days)}<br>下单截止: 提前 ${esc(j.redeem_deadline_hours)} 小时 · 时间段: ${esc(j.time_slots.join(", "))}`
                            : `${esc(j.target_date)} · ${esc(j.time_slots.join(", "))}<br>计划运行: ${esc(j.run_on_date ? `${j.run_on_date} 12:00` : "每天中午（旧任务）")} · 重试窗口: ${esc(j.retry_window_seconds)} 秒`
                        }<br>
                        策略: ${esc(slotSelectionModeLabel(j.slot_selection_mode))}<br>
                        场地: ${esc(j.preferred_fields.join(", ") || "任意")}
                    </div>
                </div>
                <span class="status-badge ${statusClass(j.last_status)}">${esc(j.last_status)}</span>
            </div>
            <div class="job-meta" style="margin-top:8px">
                ${esc(j.last_message || "—")}<br>
                检查时间: ${esc(j.last_checked_at || "从未")} · 订单: ${esc(j.last_order_id || "—")}
            </div>
            <div class="job-actions">
                <button class="btn-ghost" onclick="runJob('${j.job_id}',true)">预览</button>
                <button class="btn-run" onclick="runJob('${j.job_id}',false)">运行</button>
                <button class="btn-warn" onclick="toggleJob('${j.job_id}',${j.enabled?"false":"true"})">${j.enabled?"禁用":"启用"}</button>
                <button class="btn-danger" onclick="deleteJob('${j.job_id}')">删除</button>
            </div>
        </div>
    `).join("");
}

function renderHistory(items) {
    const el = document.getElementById("history");
    if (!items.length) { el.innerHTML = `<div class="empty">暂无活动。</div>`; return; }
    el.innerHTML = items.map(i => `
        <div class="hist-item">
            <div class="hist-msg">
                <span class="dot ${i.success?"st-ok":"st-fail"}">${i.success?"•":"×"}</span>
                <span style="${i.success?"":"color:var(--error)"}">${esc(i.job_name)}: ${esc(i.message)}</span>
            </div>
            <div class="hist-time">${esc((i.timestamp||"").substring(11,19))}</div>
        </div>
    `).join("");
}

async function refreshStatus() {
    const d = await api("/api/status");
    const jobCount = (d.jobs||[]).length;
    const enabled = (d.jobs||[]).filter(j=>j.enabled).length;
    document.getElementById("heroStatus").textContent = `${jobCount} 个任务, ${enabled} 个活跃`;
    document.getElementById("heroSub").textContent = d.next_run_at ? `下次运行: ${d.next_run_at}` : "暂无计划运行";
    document.getElementById("authMode").textContent = d.auth_mode;
    document.getElementById("nextRun").textContent = d.next_run_at ? new Date(d.next_run_at).toLocaleTimeString() : "—";
    renderJobs(d.jobs || []);
    renderHistory(d.history || []);
}

renderTimeSlots();
resetRunOnDateDefault(true);
toggleJobMode();
refreshStatus();
setInterval(refreshStatus, 15000);
</script>
</body>
</html>"""
