/* 多 Agent 协作系统前端面板 */
const $ = (sel) => document.querySelector(sel);

const state = {
  runs: [],
  selectedRun: null,
  experience: [],
  tierFilter: "all",
  upload: null,          // 已上传待发起的需求文档：{path, name}
  images: [],            // 已上传待发送的需求附图：[{path, name, bytes, dataUrl}]
  // 当前生效的 LLM 配置：模型列表按 API Key 动态获取
  llm: {
    provider: "offline", model: "", models: [],
    contextWindow: 262144, lastTraces: [],
    apiKeySet: false, apiKeyMasked: "", notice: "",
  },
  // 当前生效的技能开关（与 system.yaml 的 runtime.skills.* 同名同义）
  skills: { enabled: false, tools_enabled: false },
  // 当前生效的 ReAct 开关（enabled/max_steps/require_thought 可写；nodes/tools_nodes 只读回显）
  react: { enabled: false, max_steps: 4, require_thought: false, nodes: [], tools_nodes: [] },
};

/* 附图上传约束：与后端 mas/sources.py 的 IMAGE_SUFFIXES / MAX_IMAGES、
   frontend/server.py 的 IMAGE_MAX_BYTES（对齐 system.yaml vision.max_image_bytes 默认 5MB）一致；
   前端只做fail-fast，后端为最终校验方 */
const IMAGE_SUFFIXES = [".png", ".jpg", ".jpeg", ".webp", ".gif"];
const IMAGE_MAX_BYTES = 5 * 1024 * 1024;
const MAX_IMAGES = 4;

/* ---------------- 视图切换 ---------------- */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    $(`#view-${btn.dataset.view}`).classList.add("active");
    if (btn.dataset.view === "experience") loadExperience();
    if (btn.dataset.view === "learning") loadLearning();
  });
});

/* ---------------- 运行列表与详情 ---------------- */
async function loadRuns() {
  const res = await fetch("/api/runs");
  state.runs = await res.json();
  renderChat();   // 对话区里运行记录的状态随列表一起刷新
  const ul = $("#runs");
  ul.innerHTML = "";
  if (!state.runs.length) {
    ul.innerHTML = '<li class="empty" style="height:120px">暂无运行，先执行 python -m mas demo</li>';
    return;
  }
  state.runs.forEach((run) => {
    const running = run.finished === false;
    const canceled = !!run.canceled;   // 用户手动终止：显示「已终止」且不展示验收判定
    const li = document.createElement("li");
    li.className = "run-item" + (state.selectedRun === run.run_id ? " selected" : "");
    li.innerHTML = `
      <div class="rid">${run.run_id}</div>
      <div class="title">${escapeHtml(run.requirement_title || (running ? "（解析需求中…）" : "-"))}</div>
      <div class="meta">
        <span class="badge ${running ? "running" : canceled ? "canceled" : "done"}">${running ? "进行中" : canceled ? "已终止" : "已完成"}</span>
        ${!running && !canceled && run.verdict ? `<span class="badge ${run.verdict === "pass" ? "pass" : "fail"}">${run.verdict === "pass" ? "通过" : "未通过"}</span>` : ""}
        <span>${run.steps ?? 0} 步</span><span>${run.rollbacks ?? 0} 回退</span><span>${run.bugs_total ?? 0} 缺陷</span>
      </div>`;
    li.addEventListener("click", () => selectRun(run.run_id));
    ul.appendChild(li);
  });
  if (!state.selectedRun && state.runs.length) selectRun(state.runs[0].run_id);
}

/* ---------------- 对话区 ---------------- */
/* 一条用户消息 + 一条对应的运行记录气泡；点击气泡跳转该运行详情 */
const chatMessages = [];

function pushChatMessage(text, runId, images = []) {
  chatMessages.push({ kind: "user", text, ts: new Date(), images });
  chatMessages.push({ kind: "run", runId, ts: new Date() });
  renderChat();
}

function fmtTime(d) {
  return d.toTimeString().slice(0, 8);
}

function renderChat() {
  const box = $("#chat-messages");
  if (!chatMessages.length) {
    box.innerHTML = '<div class="empty" style="height:80px">暂无对话，输入需求后按 Enter 发送</div>';
    return;
  }
  box.innerHTML = chatMessages
    .map((m) => {
      if (m.kind === "user") {
        const imgs = (m.images || []).length
          ? `<div class="msg-imgs">${m.images
              .map((im) => `<img class="msg-img" src="${im.dataUrl}" alt="${escapeHtml(im.name)}"
                        title="${escapeHtml(im.name)}（点击查看大图）">`)
              .join("")}</div>`
          : "";
        return `<div class="msg user">
          <div class="bubble">${escapeHtml(m.text)}${imgs}</div>
          <span class="msg-time">${fmtTime(m.ts)}</span>
        </div>`;
      }
      const run = state.runs.find((r) => r.run_id === m.runId);
      const running = !run || run.finished === false;
      const canceled = !!(run && run.canceled);
      const meta = run
        ? `${run.steps ?? 0} 步 · ${run.rollbacks ?? 0} 回退 · ${run.bugs_total ?? 0} 缺陷${run.duration_s != null ? " · " + run.duration_s + "s" : ""}`
        : "已提交，等待流水线启动…";
      return `<div class="msg run-msg" data-run="${escapeHtml(m.runId)}" title="点击查看该次任务的执行情况与思考链路">
        <div class="bubble">
          <div class="msg-head">
            <span class="badge ${running ? "running" : canceled ? "canceled" : "done"}">${running ? "进行中" : canceled ? "已终止" : "已完成"}</span>
            ${run && run.verdict && !canceled ? `<span class="badge ${run.verdict === "pass" ? "pass" : "fail"}">${run.verdict === "pass" ? "通过" : "未通过"}</span>` : ""}
            <code>${escapeHtml(m.runId)}</code>
          </div>
          <div class="msg-meta">${escapeHtml(meta)}</div>
          <div class="msg-link">查看执行情况与思考链路 →</div>
        </div>
        <span class="msg-time">${fmtTime(m.ts)}</span>
      </div>`;
    })
    .join("");
}

$("#chat-messages").addEventListener("click", (ev) => {
  const img = ev.target.closest(".msg-img");
  if (img && img.src) {
    window.open(img.src, "_blank");   // dataUrl 直接在新标签页放大查看
    return;
  }
  const el = ev.target.closest(".run-msg");
  if (el && el.dataset.run) selectRun(el.dataset.run);
});

const renderCache = { runId: null, key: "" };

async function selectRun(runId) {
  state.selectedRun = runId;
  document.querySelectorAll(".run-item").forEach((el, i) => {
    el.classList.toggle("selected", state.runs[i] && state.runs[i].run_id === runId);
  });
  const res = await fetch(`/api/run?id=${encodeURIComponent(runId)}`);
  const detail = await res.json();
  // 数据无变化时跳过重绘（避免轮询打断用户展开的折叠面板/滚动位置）
  const key = `${runId}|${(detail.events || []).length}|${(detail.llm_traces || []).length}|${detail.finished}`;
  const openTraces = [];
  if (renderCache.runId === runId) {
    if (renderCache.key === key) return;
    document.querySelectorAll("#run-detail .trace").forEach((el, i) => {
      if (el.open) openTraces.push(i);
    });
  }
  renderCache.runId = runId;
  renderCache.key = key;
  renderRunDetail(detail, openTraces);
}

function renderRunDetail(detail, openTraces = []) {
  const s = detail.summary || {};
  const m = s.metrics || {};
  const iter = s.iterations || {};
  const canceled = !!detail.canceled;
  const verdictCard = canceled
    ? ["验收", "已终止", "orange"]   // 用户手动终止：无验收结论，不做通过/未通过判定
    : detail.finished === false
      ? ["验收", "进行中…", "orange"]
      : !s.verdict
        ? ["验收", "异常中断", "red"]   // 无验收结论：服务中断/线程异常，未跑完
        : ["验收", s.verdict === "pass" ? "通过" : "未通过", s.verdict === "pass" ? "green" : "red"];
  const cards = [
    verdictCard,
    ["总步数", s.steps ?? "-", ""],
    ["回退次数", s.rollbacks ?? 0, s.rollbacks ? "orange" : ""],
    ["审查驳回", s.review_rejections ?? 0, s.review_rejections ? "orange" : ""],
    ["缺陷(修复)", `${s.bugs_total ?? 0}(${s.bugs_fixed ?? 0})`, s.bugs_total ? "orange" : ""],
    ["耗时", m.duration_s != null ? m.duration_s + "s" : "-", ""],
    ["注入经验", m.injected_experiences ?? "-", ""],
  ];

  const eventRows = (detail.events || [])
    .map((e) => {
      const payload = e.payload ? JSON.stringify(e.payload) : "";
      return `<div class="event sev-${e.severity || "info"}">
        <span class="seq">#${e.seq}</span>
        <span class="type">${escapeHtml(e.type || "")}</span>
        <span class="agent">${escapeHtml([e.agent, e.node].filter(Boolean).join("@"))}</span>
        <span class="payload" title="${escapeHtml(payload)}">${escapeHtml(payload)}</span>
      </div>`;
    })
    .join("");

  const detailRunId = detail.run_id || s.run_id || "";
  const cancelPending = cancelingRuns.has(detailRunId);
  const cancelBar = detail.finished === false
    ? `<div class="detail-actions"><button id="cancel-run" class="danger-btn"
         data-run="${escapeHtml(detailRunId)}"${cancelPending ? " disabled" : ""}
         title="终止该任务：当前节点会执行完毕再停止，运行标记为「已终止」，不做验收判定">${cancelPending ? "终止中…（当前节点执行完毕后停止）" : "■ 终止任务"}</button></div>`
    : "";

  $("#run-detail").innerHTML = `
    ${cancelBar}
    <div class="cards">${cards
      .map(([label, value, cls]) => `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`)
      .join("")}</div>
    <div class="detail-block">
      <h3>执行步骤</h3>
      ${renderSteps(detail.events || [])}
    </div>
    <div class="detail-block">
      <h3>思考过程（${(detail.llm_traces || []).length} 次模型调用）</h3>
      ${renderTraces(detail.llm_traces || [])}
    </div>
    <div class="detail-block">
      <h3>事件流（${(detail.events || []).length} 条，唯一事实来源）</h3>
      <div class="timeline">${eventRows || '<div class="empty" style="height:80px">无事件</div>'}</div>
    </div>
    <div class="detail-block">
      <h3>验收报告</h3>
      <article class="markdown" style="border:none;background:transparent;padding:0">${renderMarkdown(detail.report || "")}</article>
    </div>`;
  // 恢复重绘前展开的"思考过程"面板
  document.querySelectorAll("#run-detail .trace").forEach((el, i) => {
    if (openTraces.includes(i)) el.open = true;
  });
  updateContextUsage(detail.llm_traces);   // 每次刷新同步上下文用量（运行中也会实时更新）
  if (detail.finished === false) {
    const tl = $("#run-detail .timeline");
    if (tl) tl.scrollTop = tl.scrollHeight; // 运行中自动滚动到最新事件
  }
}

/* ---------------- 终止运行任务（协作式取消，按钮在运行详情顶部） ---------------- */
/* 请求成功仅代表取消信号已送达；Leader 在当前节点执行完毕后退出并收尾（状态「已终止」），
   运行真正结束由轮询刷新呈现——期间按钮保持「终止中…」置灰态。 */
async function cancelRun(runId) {
  if (!runId) return;
  if (!window.confirm(`确认终止运行 ${runId}？\n\n终止将在当前节点执行完毕后生效；该运行标记为「已终止」，不做验收判定。`)) return;
  cancelingRuns.add(runId);
  renderCache.runId = null;   // 强制重绘：按钮立即切到「终止中…」
  if (state.selectedRun === runId) await selectRun(runId);
  try {
    const res = await fetch("/api/run/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId }),
    });
    const data = await res.json();
    if (!res.ok) {
      cancelingRuns.delete(runId);           // 信号未送达（已完成/非本进程运行）：按钮恢复
      renderCache.runId = null;
      await loadRuns();                      // 运行实际已结束：同步列表（否则停留旧的"进行中"）
      if (state.selectedRun === runId) await selectRun(runId);
      setComposerStatus(data.error || "终止失败", "error");
      return;
    }
    setComposerStatus(`已请求终止 ${runId}：等待当前节点执行完毕后停止…`, "running");
    activeRuns.add(runId);                   // 纳入轮询：结束提示「已终止」并清理置灰状态
    startPolling();
    await loadRuns();
    if (state.selectedRun === runId) { renderCache.runId = null; await selectRun(runId); }
  } catch (err) {
    cancelingRuns.delete(runId);
    setComposerStatus(`终止请求失败：${err.message}`, "error");
  }
}

$("#run-detail").addEventListener("click", (ev) => {
  const btn = ev.target.closest("#cancel-run");
  if (btn && btn.dataset.run) cancelRun(btn.dataset.run);
});

/* ---------------- 执行步骤（从事件流提炼） ---------------- */
function buildSteps(events) {
  const steps = [];
  let current = null;
  for (const e of events || []) {
    if (e.type === "node_start") {
      current = {
        node: e.node, agent: e.agent, iteration: e.iteration,
        title: (e.payload && e.payload.title) || e.node,
        status: "running", summary: "",
      };
      steps.push(current);
    } else if (e.type === "node_end") {
      const target = current || steps.filter((s) => s.node === e.node && s.status === "running").pop();
      if (target) {
        target.status = (e.payload && e.payload.status) || "pass";
        target.summary = (e.payload && e.payload.summary) || "";
        if (target === current) current = null;
      }
    } else if (e.type === "gate_evaluated") {
      const p = e.payload || {};
      const failed = (p.failed_criteria || []).map((c) => c.id).join("、");
      steps.push({
        node: e.node, agent: "leader", title: "验收门禁判定",
        status: p.passed ? "pass" : "fail",
        summary: p.passed ? "全部门禁项通过" : `未通过：${failed}`,
        criteria: p.failed_criteria || [],
      });
    } else if (e.type === "rollback") {
      const p = e.payload || {};
      steps.push({
        node: e.node, agent: "leader", title: "Leader 判定回退",
        status: "warn",
        summary: p.reason ? `${p.reason}${p.to ? " → " + p.to : ""}` : (p.to ? `回退到 ${p.to}` : JSON.stringify(p)),
      });
    } else if (e.type === "escalation") {
      steps.push({ node: e.node, agent: "leader", title: "升级策略触发", status: "fail", summary: (e.payload && e.payload.reason) || "" });
    }
  }
  return steps;
}

const STEP_ICON = { pass: "✓", fail: "✗", warn: "↩", running: "…" };

function renderSteps(events) {
  const steps = buildSteps(events);
  if (!steps.length) return '<div class="empty" style="height:60px">暂无步骤</div>';
  return `<ol class="steps">${steps
    .map(
      (s) => `<li class="step ${s.status}">
        <span class="step-icon">${STEP_ICON[s.status] || "•"}</span>
        <div class="step-body">
          <div class="step-head">
            <b>${escapeHtml(s.title)}</b>
            <span class="step-node">${escapeHtml([s.agent, s.node].filter(Boolean).join("@"))}${s.iteration ? " · 第" + s.iteration + "轮" : ""}</span>
          </div>
          ${s.summary ? `<div class="step-summary">${escapeHtml(s.summary)}</div>` : ""}
          ${(s.criteria || []).length
            ? `<ul class="criteria">${s.criteria
                .map((c) => `<li><code>${escapeHtml(c.expr || "")}</code>${c.reason ? " — " + escapeHtml(c.reason) : ""}（值: ${escapeHtml(JSON.stringify(c.value))}）</li>`)
                .join("")}</ul>`
            : ""}
        </div>
      </li>`
    )
    .join("")}</ol>`;
}

/* ---------------- 思考过程（LLM 调用 trace） ---------------- */
function renderTraces(traces) {
  if (!traces.length) {
    return '<div class="empty" style="height:60px">该运行无模型调用留痕（历史运行或未启用 LLM）</div>';
  }
  return `<div class="traces">${traces
    .map((t, i) => {
      const hasReasoning = t.reasoning && t.reasoning.trim();
      return `<details class="trace ${t.error ? "trace-error" : ""}">
        <summary>
          <span class="trace-idx">#${i + 1}</span>
          <span class="trace-agent">${escapeHtml([t.agent, t.node].filter(Boolean).join("@"))}</span>
          <span class="trace-kind">${escapeHtml(t.task_kind || "")}</span>
          ${t.error ? `<span class="badge fail">失败</span>` : ""}
          ${hasReasoning ? '<span class="badge pass">含思考链</span>' : ""}
          ${t.finish_reason === "length" ? '<span class="badge fail">输出被截断</span>' : ""}
          ${t.degraded ? '<span class="badge pass">已降级重发</span>' : ""}
          <span class="trace-meta">${t.model || ""} · ${t.tokens ?? "-"} tok${t.reasoning_tokens ? `（思考 ${t.reasoning_tokens}）` : ""} · ${((t.latency_ms || 0) / 1000).toFixed(1)}s</span>
        </summary>
        ${t.error ? `<div class="trace-section"><h4>错误</h4><pre>${escapeHtml(t.error)}</pre></div>` : ""}
        ${hasReasoning ? `<div class="trace-section"><h4>思考链（reasoning）</h4><pre>${escapeHtml(t.reasoning)}</pre></div>` : ""}
        ${t.output ? `<div class="trace-section"><h4>模型原始输出</h4><pre>${escapeHtml(t.output)}</pre></div>` : ""}
      </details>`;
    })
    .join("")}</div>`;
}

$("#refresh-runs").addEventListener("click", loadRuns);

/* ---------------- 对话入口：发起新运行并实时轮询 ---------------- */
let pollTimer = null;
/* 正在执行的运行集合：支持上一个任务进行中继续发起新需求，各自独立刷新 */
const activeRuns = new Set();
/* 已请求终止的运行集合：按钮置灰为「终止中…」，直到运行真正结束（轮询刷新后移除） */
const cancelingRuns = new Set();

/* ---------------- 模型配置弹窗：API Key / Base URL / 默认模型 + 技能开关 ---------------- */
const CFG = {
  overlay: "#settings-overlay", provider: "#cfg-provider", baseUrl: "#cfg-base-url",
  apiKey: "#cfg-api-key", model: "#cfg-model", tokenName: "#cfg-token-name",
  status: "#cfg-status", modelList: "#cfg-model-list", contextWindow: "#cfg-context-window",
  skillsEnabled: "#cfg-skills-enabled", skillsToolsEnabled: "#cfg-skills-tools-enabled",
  reactEnabled: "#cfg-react-enabled", reactRequireThought: "#cfg-react-require-thought",
  reactMaxSteps: "#cfg-react-max-steps", reactNote: "#cfg-react-note",
};

function setCfgStatus(text, cls) {
  const el = $(CFG.status);
  el.textContent = text;
  el.className = `modal-status ${cls || ""}`;
}

async function fetchLlmConfig() {
  const res = await fetch("/api/llm");
  return res.json();
}

/* 技能开关（system.yaml 的 runtime.skills.enabled / tools_enabled，字段同名同义） */
async function fetchSkillsConfig() {
  const res = await fetch("/api/skills");
  return res.json();
}

/* tools_enabled 依赖 enabled（后端双开才注册技能工具）：总开关关闭时置灰，避免误存无效组合 */
function syncSkillsSwitchState() {
  $(CFG.skillsToolsEnabled).disabled = !$(CFG.skillsEnabled).checked;
}

/* ReAct 开关（system.yaml 的 runtime.react.enabled / max_steps / require_thought，字段同名同义） */
async function fetchReactConfig() {
  const res = await fetch("/api/react");
  return res.json();
}

/* require_thought / max_steps 依赖 enabled：总开关关闭时置灰，避免存出无效组合 */
function syncReactSwitchState() {
  const on = $(CFG.reactEnabled).checked;
  $(CFG.reactRequireThought).disabled = !on;
  $(CFG.reactMaxSteps).disabled = !on;
}

/* 生效范围是三重交集 enabled ∧ nodes ∧ llm.tools_nodes：面板直接算出实际节点，
   避免"开了开关却没生效"的困惑（nodes/tools_nodes 为空均表示全部节点）。 */
function renderReactNote(data) {
  const el = $(CFG.reactNote);
  if (!data || !data.enabled) {
    el.textContent = "";
    return;
  }
  const scope = (list) => (list && list.length ? list : null);
  const nodes = scope(data.nodes);
  const tools = scope(data.tools_nodes);
  let text;
  if (!nodes && !tools) text = "将在全部节点生效";
  else if (nodes && tools) text = `将在 ${nodes.filter((n) => tools.includes(n)).join("、") || "（交集为空，不会生效）"} 节点生效`;
  else text = `将在 ${(nodes || tools).join("、")} 节点生效`;
  el.textContent = `当前：${text}（nodes × tools_nodes 取交集，两项为空表示全部节点）`;
}

/* ---------------- 主界面技能开关（与弹窗内 #cfg-skills-enabled 同源）----------------
   弹窗只在点击「⚙ 模型配置」时出现，主界面此前没有任何技能状态提示，
   因此把总开关提到发起需求的控制条上：勾选即 POST /api/skills 写回 system.yaml，
   与弹窗内的开关读写同一个字段，两处状态由下面的 sync 调用保持一致。 */
async function refreshSkillsToggle() {
  try {
    const data = await fetchSkillsConfig();
    state.skills.enabled = !!data.enabled;
    state.skills.tools_enabled = !!data.tools_enabled;
    $("#skills-toggle").checked = state.skills.enabled;
  } catch { /* 读取失败不阻塞界面，保持上一次状态 */ }
}

async function toggleSkills() {
  const el = $("#skills-toggle");
  const enabled = el.checked;
  try {
    const res = await fetch("/api/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled,
        // 总开关关闭时工具开关无意义（后端双开才注册），一并置 false
        tools_enabled: enabled ? state.skills.tools_enabled : false,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      el.checked = !enabled;
      setComposerStatus(data.error || "技能开关保存失败", "error");
      return;
    }
    state.skills.enabled = !!data.enabled;
    state.skills.tools_enabled = !!data.tools_enabled;
    el.checked = state.skills.enabled;
    // 弹窗若正开着，同步回显，避免两处状态不一致
    if (!$(CFG.overlay).hidden) {
      $(CFG.skillsEnabled).checked = state.skills.enabled;
      $(CFG.skillsToolsEnabled).checked = state.skills.tools_enabled;
      syncSkillsSwitchState();
    }
    setComposerStatus(`技能系统已${state.skills.enabled ? "开启" : "关闭"}（下一次运行生效）`, "done");
  } catch (err) {
    el.checked = !enabled;
    setComposerStatus(`技能开关保存失败：${err.message}`, "error");
  }
}

/* 用弹窗里填写的（留空则用已保存的）凭据探测 /models，拿到该 Key 支持的模型列表 */
async function fetchModels(apiKey, baseUrl) {
  const res = await fetch("/api/llm/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey || null, base_url: baseUrl || null }),
  });
  return res.json();
}

/* 按当前凭据刷新模型下拉框：所选模型不可用时自动回退到可用模型并给出提示 */
async function refreshModelList({ silent = false, preferModel = "" } = {}) {
  const llm = state.llm;
  const select = $("#llm-model-select");
  llm.notice = "";
  if (llm.provider !== "openai_compatible") {
    llm.models = [];
    select.innerHTML = '<option value="">offline-deterministic（内置确定性）</option>';
    return true;
  }
  try {
    const data = await fetchModels();
    if (!data.ok) {
      select.innerHTML = `<option value="${escapeHtml(llm.model)}">${escapeHtml(llm.model || "当前模型")}（列表获取失败）</option>`;
      llm.notice = `模型列表获取失败：${data.error || "未知错误"}`;
      if (!silent) setComposerStatus(`${llm.notice}（到「模型配置」检查 Key / Base URL）`, "error");
      return false;
    }
    llm.models = (data.models || []).filter(Boolean);
    select.innerHTML = llm.models.length
      ? llm.models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("")
      : `<option value="${escapeHtml(llm.model)}">${escapeHtml(llm.model || "未获取到模型")}</option>`;
    const want = preferModel || llm.model;
    if (want && llm.models.includes(want)) {
      select.value = want;
      llm.model = want;
    } else {
      const fallback = llm.models[0] || want || "";
      if (want && fallback && fallback !== want) {
        llm.notice = `模型 ${want} 不在当前 Key 的可用列表中，已自动回退到 ${fallback}`;
        if (!silent) setComposerStatus(llm.notice, "error");
      }
      if (fallback) { select.value = fallback; llm.model = fallback; }
    }
    return true;
  } catch (err) {
    llm.notice = `模型列表获取失败：${err.message}`;
    if (!silent) setComposerStatus(llm.notice, "error");
    return false;
  }
}

async function initLlmControls() {
  try {
    const cfg = await fetchLlmConfig();
    state.llm.provider = cfg.provider || "offline";
    state.llm.model = cfg.model || "";
    state.llm.contextWindow = cfg.context_window || 262144;   // 端点不返回上下文长度，由弹窗维护
    state.llm.apiKeySet = !!cfg.api_key_set;
    state.llm.apiKeyMasked = cfg.api_key_masked || "";
    await refreshModelList({ silent: true });
  } catch { /* 保持默认 offline */ }
  // 仅当还没有任何运行用量时渲染占位，避免覆盖已选中运行（loadRuns 可能先于本函数完成）
  if (!state.llm.lastTraces.length) updateContextUsage([]);
}

/* 打开弹窗：回显当前生效的凭据、模型与技能开关 */
function openSettings() {
  setCfgStatus("正在读取当前配置……", "");
  Promise.all([
    fetchLlmConfig(),
    fetchSkillsConfig().catch(() => null),   // 技能开关读取失败不阻塞弹窗，保存仍按表单值写入
    fetchReactConfig().catch(() => null),    // ReAct 开关同理
  ]).then(([cfg, skills, react]) => {
    state.llm.apiKeySet = !!cfg.api_key_set;
    state.llm.apiKeyMasked = cfg.api_key_masked || "";
    $(CFG.provider).value = cfg.provider || "offline";
    $(CFG.baseUrl).value = cfg.base_url || "";
    $(CFG.apiKey).value = cfg.api_key || "";
    $(CFG.model).value = cfg.model || "";
    $(CFG.tokenName).value = cfg.token_name || "";
    $(CFG.contextWindow).value = cfg.context_window || 262144;
    $(CFG.modelList).innerHTML = (cfg.model_presets || [])
      .map((m) => `<option value="${escapeHtml(m)}">`).join("");
    $(CFG.apiKey).placeholder = cfg.api_key
      ? "输入新的 API Key（留空表示沿用已保存值）"
      : (cfg.api_key_masked ? `已保存 ${cfg.api_key_masked}（留空沿用）` : "输入你的 API Key");
    if (skills) {
      $(CFG.skillsEnabled).checked = !!skills.enabled;
      $(CFG.skillsToolsEnabled).checked = !!skills.tools_enabled;
      syncSkillsSwitchState();
      state.skills.enabled = !!skills.enabled;
      state.skills.tools_enabled = !!skills.tools_enabled;
      $("#skills-toggle").checked = state.skills.enabled;   // 弹窗打开即与主界面开关对齐
    }
    if (react) {
      state.react.enabled = !!react.enabled;
      state.react.max_steps = react.max_steps || 4;
      state.react.nodes = react.nodes || [];
      state.react.tools_nodes = react.tools_nodes || [];
      $(CFG.reactEnabled).checked = !!react.enabled;
      $(CFG.reactRequireThought).checked = !!react.require_thought;
      $(CFG.reactMaxSteps).value = react.max_steps || 4;
      syncReactSwitchState();
      renderReactNote(react);
    }
    setCfgStatus(cfg.api_key_set ? `当前凭据：${cfg.api_key_masked}` : "当前未设置 API Key", "");
  }).catch(() => setCfgStatus("读取当前配置失败，请检查服务是否可用", "error"));
  $(CFG.overlay).hidden = false;
}

function closeSettings() { $(CFG.overlay).hidden = true; }

function validateSettings(provider, baseUrl, apiKey, model, contextWindow) {
  if (provider === "openai_compatible") {
    if (!/^https?:\/\/\S+/i.test(baseUrl || "")) return "接口地址必须以 http(s):// 开头";
    if (!apiKey && !state.llm.apiKeySet) return "API Key 不能为空";
    if (!model) return "默认模型不能为空";
  }
  if (contextWindow && (!Number.isInteger(Number(contextWindow)) || Number(contextWindow) < 1024)) {
    return "上下文窗口必须是不小于 1024 的整数（平台当前不超过 256K = 262144）";
  }
  return "";
}

$("#open-settings").addEventListener("click", openSettings);
$("#settings-close").addEventListener("click", closeSettings);
$("#cfg-cancel").addEventListener("click", closeSettings);
$(CFG.overlay).addEventListener("click", (ev) => { if (ev.target === $(CFG.overlay)) closeSettings(); });
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !$(CFG.overlay).hidden) closeSettings();
});

/* 技能总开关联动：关闭时置灰「技能工具调用」勾选框（值保留，后端双开才生效） */
$(CFG.skillsEnabled).addEventListener("change", syncSkillsSwitchState);

/* ReAct 总开关联动：关闭时置灰「强制思考」与「最大步数」，并重算生效节点提示 */
$(CFG.reactEnabled).addEventListener("change", () => {
  syncReactSwitchState();
  renderReactNote({ enabled: $(CFG.reactEnabled).checked, nodes: state.react.nodes,
                    tools_nodes: state.react.tools_nodes });
});

/* 主界面技能开关：勾选即时写回 system.yaml */
$("#skills-toggle").addEventListener("change", toggleSkills);

/* 显示/隐藏 API Key */
$("#cfg-eye").addEventListener("click", () => {
  const el = $(CFG.apiKey);
  el.type = el.type === "password" ? "text" : "password";
});

/* 测试连接：顺带把该 Key 的模型列表灌进默认模型的下拉 */
$("#cfg-test").addEventListener("click", async () => {
  const apiKey = $(CFG.apiKey).value.trim();
  const baseUrl = $(CFG.baseUrl).value.trim();
  setCfgStatus("正在测试连接……", "running");
  let data;
  try {
    data = await fetchModels(apiKey || null, baseUrl || null);
  } catch (err) {
    data = { ok: false, error: err.message };
  }
  if (!data.ok) {
    setCfgStatus(`连接失败：${data.error}`, "error");
    return;
  }
  const models = data.models || [];
  $(CFG.modelList).innerHTML = models.map((m) => `<option value="${escapeHtml(m)}">`).join("");
  const current = $(CFG.model).value.trim();
  if (models.length && !models.includes(current)) $(CFG.model).value = models[0];
  setCfgStatus(
    `连接成功：${data.base_url}（Key ${data.api_key_masked}）· 可用模型 ${models.length} 个：${models.slice(0, 8).join(" / ")}`,
    "done"
  );
});

/* 保存：写回配置凭据，关闭后立即生效；Key 变更或模型不可用时自动回退 */
$("#cfg-save").addEventListener("click", async () => {
  const provider = $(CFG.provider).value;
  const baseUrl = $(CFG.baseUrl).value.trim();
  const apiKey = $(CFG.apiKey).value.trim();
  const model = $(CFG.model).value.trim();
  const tokenName = $(CFG.tokenName).value.trim();
  const contextWindow = $(CFG.contextWindow).value.trim();
  const problem = validateSettings(provider, baseUrl, apiKey, model, contextWindow);
  if (problem) {
    setCfgStatus(problem, "error");
    return;
  }
  setCfgStatus("正在保存……", "running");
  try {
    const res = await fetch("/api/llm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider,
        model: model || null,
        api_key: apiKey || null,      // 留空表示沿用已保存的 Key
        base_url: baseUrl || null,
        token_name: tokenName || null,
        context_window: contextWindow ? Number(contextWindow) : null,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      setCfgStatus(data.error || "保存失败", "error");
      return;
    }
    const keyChanged = !!apiKey;
    state.llm.provider = data.provider || provider;
    state.llm.model = data.model || model;
    state.llm.contextWindow = data.context_window || state.llm.contextWindow;
    updateContextUsage(state.llm.lastTraces);   // 改了上下文窗口立即重算进度条
    // 技能开关与模型配置同一次保存写回 system.yaml（下一次运行生效，无需重启服务）
    const skillsRes = await fetch("/api/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $(CFG.skillsEnabled).checked,
        tools_enabled: $(CFG.skillsToolsEnabled).checked,
      }),
    });
    const skillsData = await skillsRes.json();
    if (!skillsRes.ok) {
      setCfgStatus(skillsData.error || "技能开关保存失败（模型配置已保存）", "error");
      return;
    }
    // ReAct 开关同上：与模型配置、技能开关同一次保存写回 system.yaml
    const reactRes = await fetch("/api/react", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $(CFG.reactEnabled).checked,
        max_steps: Number($(CFG.reactMaxSteps).value) || 4,
        require_thought: $(CFG.reactRequireThought).checked,
      }),
    });
    const reactData = await reactRes.json();
    if (!reactRes.ok) {
      setCfgStatus(reactData.error || "ReAct 开关保存失败（模型配置与技能开关已保存）", "error");
      return;
    }
    // 弹窗保存后同步主界面开关（两处读写同一字段）
    state.skills.enabled = !!skillsData.enabled;
    state.skills.tools_enabled = !!skillsData.tools_enabled;
    $("#skills-toggle").checked = state.skills.enabled;
    setCfgStatus(
      `已保存：${data.provider}${data.model ? " / " + data.model : ""}${data.api_key_masked ? " · Key " + data.api_key_masked : ""}` +
      `｜技能 ${skillsData.enabled ? "开" : "关"}${skillsData.tools_enabled ? "（含工具调用）" : ""}` +
      `｜ReAct ${reactData.enabled ? "开" : "关"}${reactData.enabled ? `（≤${reactData.max_steps} 步）` : ""}`,
      "done"
    );
    await refreshModelList({ preferModel: state.llm.model });   // 保存后按新 Key 重取模型，必要时自动回退
    closeSettings();
    if (!state.llm.notice) {
      setComposerStatus(`配置已生效：${state.llm.model || state.llm.provider}${keyChanged ? "（Key 已更新）" : ""}`, "done");
    }
  } catch (err) {
    setCfgStatus(`保存失败：${err.message}`, "error");
  }
});

/* 手动刷新模型列表 */
$("#reload-models").addEventListener("click", async () => {
  setComposerStatus("正在获取可用模型……", "running");
  const ok = await refreshModelList();
  if (ok && !state.llm.notice) setComposerStatus(`已更新可用模型：${state.llm.models.length} 个`, "done");
});

/* 对话窗口切换模型 */
$("#llm-model-select").addEventListener("change", () => {
  state.llm.model = $("#llm-model-select").value;
  setComposerStatus(`本次运行将使用模型：${state.llm.model || "（内置确定性）"}`, "");
});

/* ---------------- 上下文用量（已用 / 总量 / 占比进度条） ---------------- */
function fmtTokens(n) {
  const v = Number(n) || 0;
  // 按 1024 进位显示成 K，与平台"上下文不超过 256K"的口径一致
  if (v >= 1024) return `${(v / 1024).toFixed(v >= 10240 ? 0 : 1)}K`;
  return String(v);
}

function updateContextUsage(traces) {
  const list = traces || [];
  state.llm.lastTraces = list;                  // 记住最近一次用量，改配置后可立即重算
  // 取单次调用 token 峰值，代表该模型一次请求占用的上下文
  const used = list.reduce((max, t) => Math.max(max, Number(t && t.tokens) || 0), 0);
  const model = (list.length && list[list.length - 1].model) || state.llm.model || "";
  const total = state.llm.contextWindow || 262144;
  const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
  const box = $("#context-usage");
  box.classList.toggle("warn", pct >= 80 && pct < 95);
  box.classList.toggle("danger", pct >= 95);
  box.querySelector(".ctx-fill").style.width = `${pct.toFixed(1)}%`;
  const tip = pct >= 95 ? " · 接近上限，请精简需求或拆分运行" : (pct >= 80 ? " · 已接近上限" : "");
  box.querySelector(".ctx-text").textContent = list.length
    ? `上下文 ${fmtTokens(used)} / ${fmtTokens(total)}（${pct.toFixed(1)}%）${tip}`
    : `上下文 0 / ${fmtTokens(total)}（当前运行无模型调用）`;
}

function setComposerStatus(text, cls) {
  const el = $("#composer-status");
  el.textContent = text;
  el.className = `composer-status ${cls || ""}`;
}

async function startRun() {
  const requirement = $("#new-requirement").value.trim();
  const upload = state.upload;
  const images = state.images;
  if (!requirement && !upload) {
    setComposerStatus("请先输入需求描述或上传需求文档（图片作为附图随需求一起发送）", "error");
    return;
  }
  const provider = state.llm.provider || "offline";
  const model = $("#llm-model-select").value.trim();
  const btn = $("#start-run");
  btn.disabled = true;   // 仅在提交请求期间禁用，发起后立即可继续输入新需求
  setComposerStatus("正在启动流水线……", "running");
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        // 上传文档与文字需求二选一（后端对「都给/都空」返回 400）；图片为附图，两者都可兼带
        requirement: upload ? "" : requirement,
        requirement_path: upload ? upload.path : null,
        images: images.length ? images.map((i) => i.path) : null,
        profile: $("#new-profile").value || null,
        provider,
        model: provider === "openai_compatible" ? model : null,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      setComposerStatus(data.error || "启动失败", "error");
      return;
    }
    $("#new-requirement").value = "";                 // 发送后清空输入框
    pushChatMessage(upload ? `［上传文档］${upload.name}` : requirement, data.run_id, images);
    state.upload = null;
    state.images = [];
    $("#upload-doc").value = "";
    $("#upload-image").value = "";
    setUploadName("");
    syncUploadUi();                                   // 已发送：隐藏「取消上传」按钮
    renderImageTray();
    state.selectedRun = data.run_id;
    activeRuns.add(data.run_id);
    await loadRuns();
    await selectRun(data.run_id);
    setComposerStatus(`运行中 ${data.run_id} …（可继续发起新需求）`, "running");
    startPolling();
  } catch (err) {
    setComposerStatus(`请求失败：${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollTick, 1500);
}

async function pollTick() {
  await loadRuns();   // 列表里的"进行中 / 已完成 / 已终止"随轮询刷新
  for (const runId of Array.from(activeRuns)) {
    const run = state.runs.find((r) => r.run_id === runId);
    if (run && run.finished !== false) {
      activeRuns.delete(runId);
      cancelingRuns.delete(runId);   // 收尾置灰状态（按钮只在运行中出现，此处仅为状态清理）
      if (run.canceled) {
        setComposerStatus(`已终止 ${runId}：用户手动终止（第 ${run.steps} 步后停止，未参与验收判定）`, "");
      } else {
        setComposerStatus(
          `完成 ${runId}：${run.verdict === "pass" ? "验收通过" : "验收未通过"}（${run.steps} 步 / ${run.rollbacks} 回退 / ${run.bugs_total} 缺陷）`,
          run.verdict === "pass" ? "done" : "error"
        );
      }
    }
  }
  if (state.selectedRun) await selectRun(state.selectedRun);   // 刷新当前查看详情（含思考链路）
  if (!activeRuns.size && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

$("#start-run").addEventListener("click", startRun);
$("#new-requirement").addEventListener("keydown", (ev) => {
  // Enter 发送，Shift+Enter 换行（Ctrl/Cmd+Enter 仍保留为发送快捷键）
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    startRun();
  }
});
$("#load-example").addEventListener("click", async () => {
  const res = await fetch("/api/example");
  const text = await res.text();
  if (text) {
    $("#new-requirement").value = text;
    setComposerStatus("已载入示例需求模板，可直接发起或在此基础上修改", "");
  } else {
    setComposerStatus("未找到示例文件", "error");
  }
});

/* ---------------- 需求文档上传（两段式：/api/upload → /api/run 带 requirement_path） ---------------- */
function setUploadName(text, cls) {
  const el = $("#upload-name");
  el.textContent = text || "";
  el.className = `composer-status ${cls || ""}`;
}

/* 「取消上传」按钮跟随 state.upload 显隐：仅在已选择待发送文档时出现 */
function syncUploadUi() {
  $("#cancel-upload").hidden = !state.upload;
}

/* 取消本次上传：清前端已选状态（回到未上传状态）+ 删除服务端暂存文件。
   删除失败不回滚前端取消——本地原文件仍在，服务端残留只是一次多余的暂存副本；
   文件此前已不存在时按幂等处理（removed=false，不视为错误）。 */
async function cancelUpload() {
  const upload = state.upload;
  if (!upload) return;
  state.upload = null;
  syncUploadUi();
  $("#upload-doc").value = "";   // 允许重新选择同一文件
  try {
    const res = await fetch("/api/upload/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: upload.path }),
    });
    const data = await res.json();
    if (res.ok) {
      setUploadName(data.removed === false
        ? `已取消本次上传：${upload.name}（暂存文件此前已不存在）`
        : `已取消本次上传：${upload.name}（暂存文件已删除）`);
    } else {
      setUploadName(`已取消选择：${upload.name}（暂存文件未删除：${data.error || "未知错误"}）`, "error");
    }
  } catch (err) {
    setUploadName(`已取消选择：${upload.name}（暂存文件未删除：${err.message}）`, "error");
  }
}

$("#cancel-upload").addEventListener("click", cancelUpload);

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      resolve(result.slice(result.indexOf(",") + 1));   // 去掉 data:...;base64, 前缀
    };
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}

/* File → /api/upload（base64 JSON）→ state.upload（发送时走 requirement_path 文件通道）。
   nameOverride 供粘贴场景使用：剪贴板文件的 name 可能为空（截图等途径），
   此时由调用方按 MIME 补一个带后缀的名字，避免后端以「文件名非法」明确拒绝。 */
async function uploadDocFile(file, nameOverride) {
  const name = nameOverride || file.name;
  if (file.size > 10 * 1024 * 1024) {
    setUploadName("文件超过 10MB 上限", "error");
    return false;
  }
  setUploadName(`正在上传 ${name} ……`, "running");
  try {
    const content_base64 = await fileToBase64(file);
    const res = await fetch("/api/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: name, content_base64 }),
    });
    const data = await res.json();
    if (!res.ok) {
      state.upload = null;
      syncUploadUi();
      setUploadName(data.error || "上传失败", "error");
      return false;
    }
    state.upload = { path: data.path, name: data.name };
    syncUploadUi();   // 出现「取消上传」按钮（上传错误时可撤销本次上传）
    setUploadName(`已选择：${data.name}（${data.kind}，发送时将替代输入框内容）`, "done");
    return true;
  } catch (err) {
    state.upload = null;
    syncUploadUi();
    setUploadName(`上传失败：${err.message}`, "error");
    return false;
  }
}

$("#upload-doc").addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = "";   // 同一文件可重复选择（与 #upload-image 一致）
  if (!file) return;
  await uploadDocFile(file);
});

/* ---------------- 需求附图上传（两段式：/api/upload_image → /api/run 带 images） ---------------- */
/* 图片是需求文字/文档的附图：随需求进入流水线（存档 + requirement_images_attached 事件），
   单独发送图片不构成可执行需求，因此发送前仍要求有文字或文档（见 startRun 的守卫）。 */
function renderImageTray() {
  const tray = $("#image-tray");
  tray.hidden = !state.images.length;
  tray.innerHTML = state.images
    .map((img, i) => `
      <div class="img-chip" title="${escapeHtml(img.name)}（${img.bytes} 字节，随需求一起发送）">
        <img src="${img.dataUrl}" alt="${escapeHtml(img.name)}">
        <button type="button" data-idx="${i}" title="移除该图片">✕</button>
      </div>`)
    .join("");
}

$("#image-tray").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-idx]");
  if (!btn) return;
  state.images.splice(Number(btn.dataset.idx), 1);
  renderImageTray();
});

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}

/* File → /api/upload_image → state.images + 缩略图托盘（随需求一起进入流水线）。
   nameOverride 供粘贴场景使用（见 uploadDocFile 同名参数说明）。
   返回 false 表示该文件未入队（校验失败/上传出错），调用方据此中止整批（与改造前一致）。 */
async function uploadImageFile(file, nameOverride) {
  const name = nameOverride || file.name;
  const suffix = "." + (name.split(".").pop() || "").toLowerCase();
  if (!IMAGE_SUFFIXES.includes(suffix)) {
    setUploadName(`不支持的图片格式 ${suffix || "(无后缀)"}：允许 png/jpg/jpeg/webp/gif`, "error");
    return false;
  }
  if (file.size > IMAGE_MAX_BYTES) {
    setUploadName(`${name} 超过单张 5MB 上限`, "error");
    return false;
  }
  if (state.images.length >= MAX_IMAGES) {
    setUploadName(`最多上传 ${MAX_IMAGES} 张附图`, "error");
    return false;
  }
  setUploadName(`正在上传 ${name} ……`, "running");
  try {
    const dataUrl = await fileToDataUrl(file);
    const res = await fetch("/api/upload_image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: name,
        content_base64: dataUrl.slice(dataUrl.indexOf(",") + 1),
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      setUploadName(data.error || "上传失败", "error");
      return false;
    }
    state.images.push({ path: data.path, name: data.name, bytes: data.bytes, dataUrl });
    return true;
  } catch (err) {
    setUploadName(`上传失败：${err.message}`, "error");
    return false;
  }
}

$("#upload-image").addEventListener("change", async (ev) => {
  const files = Array.from(ev.target.files || []);
  ev.target.value = "";   // 同一文件可重复选择
  if (!files.length) return;
  for (const file of files) {
    if (!(await uploadImageFile(file))) return;   // 出错即中止整批（与改造前一致）
  }
  renderImageTray();
  setUploadName(`已选择 ${state.images.length} 张附图（随需求一起发送，可点 ✕ 移除）`, "done");
});

/* ---------------- 对话窗口直接粘贴（截图 / 剪贴板文件） ---------------- */
/* 粘贴只是新增一个「文件来源」，复用按钮上传的同两条通道，后端零改动：
   图片（截图、复制的图片文件）→ uploadImageFile → /api/upload_image → 需求附图；
   其他文件（复制的需求文档）→ uploadDocFile → /api/upload → 需求文档。
   校验 / 落盘 / 入流水线仍由后端既有链路负责（save_upload → Leader.run → prepare_images），
   本段只做「取文件 → 分类 → 转交」。clipboardData 无文件时放行默认行为（纯文本正常粘贴）。 */
function clipboardFiles(data) {
  const direct = Array.from(data.files || []);
  if (direct.length) return direct;
  // 兜底：个别浏览器 files 为空但 items 里有文件项
  return Array.from(data.items || [])
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter(Boolean);
}

/* 剪贴板文件的 name 可能为空（截图、部分复制途径）：按 MIME 补一个带后缀的名字，
   否则后端会以「文件名非法 / 不支持的后缀」明确拒绝（明确失败而非静默）。 */
function pastedFileName(file, fallbackExt) {
  if (file.name && /\.[A-Za-z0-9]+$/.test(file.name)) return file.name;
  const ext = ((file.type || "").split("/")[1] || fallbackExt || "")
    .replace(/[^a-z0-9]/gi, "");
  return ext ? `pasted-${Date.now()}.${ext}` : `pasted-${Date.now()}`;
}

$("#new-requirement").addEventListener("paste", async (ev) => {
  const files = clipboardFiles(ev.clipboardData);
  if (!files.length) return;   // 无文件：默认行为，纯文本正常插入输入框
  ev.preventDefault();
  let imagesAdded = 0;
  for (const file of files) {
    // 分类：MIME 或后缀任一指向图片即走附图通道（剪贴板截图常只有 MIME 可靠）
    const isImage = (file.type || "").startsWith("image/")
      || IMAGE_SUFFIXES.includes("." + (file.name.split(".").pop() || "").toLowerCase());
    const ok = isImage
      ? await uploadImageFile(file, pastedFileName(file, "png"))
      : await uploadDocFile(file, pastedFileName(file, ""));
    if (ok && isImage) imagesAdded += 1;
  }
  renderImageTray();   // 与按钮路径一致：粘贴的图片也要进托盘可见、可移除
  if (imagesAdded) {
    setUploadName(`已选择 ${state.images.length} 张附图（随需求一起发送，可点 ✕ 移除）`, "done");
  }
});

/* ---------------- 经验库 ---------------- */
async function loadExperience() {
  const res = await fetch("/api/experience");
  state.experience = await res.json();
  renderExperience();
}

$("#tier-filters").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".chip");
  if (!btn) return;
  state.tierFilter = btn.dataset.tier;
  document.querySelectorAll("#tier-filters .chip").forEach((c) => c.classList.toggle("active", c === btn));
  renderExperience();
});

function renderExperience() {
  const entries = state.experience;
  const filtered = state.tierFilter === "all" ? entries : entries.filter((e) => e.tier === state.tierFilter);
  const count = (tier) => entries.filter((e) => e.tier === tier).length;
  const totalHits = entries.reduce((acc, e) => acc + (e.hits || 0), 0);

  $("#experience-stats").innerHTML = `
    <div class="stat-pill">总计<b>${entries.length}</b></div>
    <div class="stat-pill">L1<b>${count("L1")}</b></div>
    <div class="stat-pill">L2<b>${count("L2")}</b></div>
    <div class="stat-pill">L3<b>${count("L3")}</b></div>
    <div class="stat-pill">累计命中<b>${totalHits}</b></div>`;

  $("#experience-list").innerHTML = filtered.length
    ? filtered
        .map(
          (e) => `
      <div class="exp-item">
        <div class="exp-head">
          <span class="tier ${e.tier}">${e.tier}</span>
          <span class="exp-id">${e.id}</span>
          <span class="exp-meta">置信度 ${(e.confidence ?? 0).toFixed(2)} · 命中 ${e.hits ?? 0} · 证据 ${e.evidence_count ?? 0} · ${escapeHtml((e.roles || []).join("/"))}</span>
        </div>
        <div class="exp-lesson">${escapeHtml(e.lesson || "")}</div>
        ${e.action ? `<div class="exp-action">动作：${escapeHtml(e.action)}（${escapeHtml(e.action_key || "")}）</div>` : ""}
        <div class="exp-tags">${(e.tags || []).slice(0, 8).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>
      </div>`
        )
        .join("")
    : '<div class="empty">暂无经验条目</div>';
}

/* ---------------- 自学习报告 ---------------- */
async function loadLearning() {
  const [metricsRes, reportRes] = await Promise.all([fetch("/api/metrics"), fetch("/api/learning-report")]);
  const metrics = await metricsRes.json();
  const report = await reportRes.text();
  renderMetricsChart(metrics);
  $("#learning-report").innerHTML = renderMarkdown(report || "暂无自学习报告（至少运行两轮流水线后生成）");
}

function renderMetricsChart(metrics) {
  const el = $("#metrics-chart");
  if (!metrics.length) {
    el.innerHTML = '<div class="empty" style="height:80px">暂无指标数据</div>';
    return;
  }
  const max = Math.max(...metrics.map((m) => m.node_executions || 0), 1);
  el.innerHTML = `
    <div class="chart-title">各轮运行节点执行步数（自学习收益：步数越低越好）</div>
    <div class="bars">${metrics
      .map((m) => {
        const h = Math.max(4, Math.round(((m.node_executions || 0) / max) * 120));
        return `<div class="bar-group">
          <span class="bar-value">${m.node_executions}</span>
          <div class="bar" style="height:${h}px" title="${m.run_id}：回退 ${m.rollbacks}，缺陷 ${m.bugs_total}"></div>
          <span class="bar-label">${(m.run_id || "").slice(4, 12)}</span>
        </div>`;
      })
      .join("")}</div>`;
}

/* ---------------- 极简 Markdown 渲染 ---------------- */
function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderMarkdown(md) {
  if (!md) return '<div class="empty" style="height:80px">无内容</div>';
  const lines = md.split(/\r?\n/);
  const html = [];
  let inTable = false;
  let inCode = false;
  let inList = false;

  const inline = (t) =>
    escapeHtml(t)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

  const closeBlocks = () => {
    if (inTable) { html.push("</table>"); inTable = false; }
    if (inList) { html.push("</ul>"); inList = false; }
  };

  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inCode) { html.push("</code></pre>"); inCode = false; }
      else { closeBlocks(); html.push("<pre><code>"); inCode = true; }
      continue;
    }
    if (inCode) { html.push(escapeHtml(line) + "\n"); continue; }

    if (/^\s*\|.*\|\s*$/.test(line)) {
      const cells = line.trim().slice(1, -1).split("|").map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
      if (!inTable) { closeBlocks(); html.push("<table>"); inTable = true; }
      const isHeader = !html[html.length - 1].includes("<tr>");
      const tag = isHeader ? "th" : "td";
      html.push(`<tr>${cells.map((c) => `<${tag}>${inline(c)}</${tag}>`).join("")}</tr>`);
      continue;
    }
    closeBlocks();

    if (/^###\s/.test(line)) html.push(`<h3>${inline(line.slice(4))}</h3>`);
    else if (/^##\s/.test(line)) html.push(`<h2>${inline(line.slice(3))}</h2>`);
    else if (/^#\s/.test(line)) html.push(`<h1>${inline(line.slice(2))}</h1>`);
    else if (/^\s*[-*]\s/.test(line)) {
      if (!inList) { html.push("<ul>"); inList = true; }
      html.push(`<li>${inline(line.replace(/^\s*[-*]\s/, ""))}</li>`);
    } else if (line.trim() === "") html.push("");
    else html.push(`<p>${inline(line)}</p>`);
  }
  closeBlocks();
  return html.join("\n");
}

/* ---------------- 启动 ---------------- */
initLlmControls();
refreshSkillsToggle();
loadRuns();
