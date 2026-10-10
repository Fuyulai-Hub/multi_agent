/* 多 Agent 协作系统前端面板 */
const $ = (sel) => document.querySelector(sel);

const state = {
  runs: [],
  selectedRun: null,
  experience: [],
  tierFilter: "all",
  // 当前生效的 LLM 配置：模型列表按 API Key 动态获取
  llm: {
    provider: "offline", model: "", models: [],
    contextWindow: 262144, lastTraces: [],
    apiKeySet: false, apiKeyMasked: "", notice: "",
  },
};

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
  const ul = $("#runs");
  ul.innerHTML = "";
  if (!state.runs.length) {
    ul.innerHTML = '<li class="empty" style="height:120px">暂无运行，先执行 python -m mas demo</li>';
    return;
  }
  state.runs.forEach((run) => {
    const li = document.createElement("li");
    li.className = "run-item" + (state.selectedRun === run.run_id ? " selected" : "");
    li.innerHTML = `
      <div class="rid">${run.run_id}</div>
      <div class="title">${escapeHtml(run.requirement_title || "-")}</div>
      <div class="meta">
        <span class="badge ${run.verdict === "pass" ? "pass" : "fail"}">${run.verdict === "pass" ? "通过" : "未通过"}</span>
        <span>${run.steps} 步</span><span>${run.rollbacks} 回退</span><span>${run.bugs_total} 缺陷</span>
      </div>`;
    li.addEventListener("click", () => selectRun(run.run_id));
    ul.appendChild(li);
  });
  if (!state.selectedRun && state.runs.length) selectRun(state.runs[0].run_id);
}

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
  const verdictCard = detail.finished === false
    ? ["验收", "进行中…", "orange"]
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

  $("#run-detail").innerHTML = `
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
          <span class="trace-meta">${t.model || ""} · ${t.tokens ?? "-"} tok · ${((t.latency_ms || 0) / 1000).toFixed(1)}s</span>
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

/* ---------------- 模型配置弹窗：API Key / Base URL / 默认模型 ---------------- */
const CFG = {
  overlay: "#settings-overlay", provider: "#cfg-provider", baseUrl: "#cfg-base-url",
  apiKey: "#cfg-api-key", model: "#cfg-model", tokenName: "#cfg-token-name",
  status: "#cfg-status", modelList: "#cfg-model-list", contextWindow: "#cfg-context-window",
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

/* 打开弹窗：回显当前生效的凭据与模型 */
function openSettings() {
  setCfgStatus("正在读取当前配置……", "");
  fetchLlmConfig().then((cfg) => {
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
    setCfgStatus(
      `已保存：${data.provider}${data.model ? " / " + data.model : ""}${data.api_key_masked ? " · Key " + data.api_key_masked : ""}`,
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
  if (!requirement) {
    setComposerStatus("请先输入需求描述", "error");
    return;
  }
  const provider = state.llm.provider || "offline";
  const model = $("#llm-model-select").value.trim();
  const btn = $("#start-run");
  btn.disabled = true;
  setComposerStatus("正在启动流水线……", "running");
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requirement,
        profile: $("#new-profile").value || null,
        provider,
        model: provider === "openai_compatible" ? model : null,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      setComposerStatus(data.error || "启动失败", "error");
      btn.disabled = false;
      return;
    }
    state.selectedRun = data.run_id;
    setComposerStatus(`运行中 ${data.run_id} …`, "running");
    pollRun(data.run_id);
  } catch (err) {
    setComposerStatus(`请求失败：${err.message}`, "error");
    btn.disabled = false;
  }
}

async function pollRun(runId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (state.selectedRun !== runId) {
      // 用户已手动切换到其他运行，停止跟随本轮轮询
      clearInterval(pollTimer);
      pollTimer = null;
      $("#start-run").disabled = false;
      return;
    }
    await loadRuns();
    await selectRun(runId); // 运行中也能展示实时事件流（summary 未生成时显示"进行中"）
    const run = state.runs.find((r) => r.run_id === runId);
    if (run && run.verdict) {
      clearInterval(pollTimer);
      pollTimer = null;
      $("#start-run").disabled = false;
      setComposerStatus(
        `完成 ${runId}：${run.verdict === "pass" ? "验收通过" : "验收未通过"}（${run.steps} 步 / ${run.rollbacks} 回退 / ${run.bugs_total} 缺陷）`,
        run.verdict === "pass" ? "done" : "error"
      );
    }
  }, 1500);
}

$("#start-run").addEventListener("click", startRun);
$("#new-requirement").addEventListener("keydown", (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") startRun();
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
loadRuns();
