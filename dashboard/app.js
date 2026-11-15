/* KSENUS dashboard client.
 * Talks only to the local API. Never receives session strings or API keys.
 */

const state = {
  token: "",
  confirmationId: null,
  conversationId: null,
};

const $ = (id) => document.getElementById(id);

function apiToken() {
  const stored = window.localStorage.getItem("ksenus_api_token");
  if (stored) return stored;
  const entered = window.prompt(
    "This agent has API_TOKEN set. Paste it for this browser session (stored locally)."
  );
  if (entered) {
    window.localStorage.setItem("ksenus_api_token", entered.trim());
    return entered.trim();
  }
  return "";
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (state.token) headers["X-API-Token"] = state.token;
  if (options.body && typeof options.body === "string") {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  const response = await fetch(path, Object.assign({}, options, { headers }));
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const detail = (body && body.detail) || response.statusText || "Request failed";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmtDate(value) {
  if (!value) return "no deadline";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function renderServices(services) {
  const labels = {
    brain: "AI brain",
    tasks: "Tasks",
    scheduler: "Scheduler",
    telegram_messaging: "Telegram messaging",
    telegram_calls_signaling: "Call signaling",
    telegram_calls_audio: "Call audio",
    speech_to_text: "Speech to text",
    text_to_speech: "Text to speech",
    dashboard: "Dashboard",
  };
  const grid = $("service-grid");
  grid.replaceChildren();
  Object.entries(services).forEach(([key, ok]) => {
    const row = el("div", "service");
    row.append(el("span", "", labels[key] || key));
    row.append(el("span", ok ? "ok" : "off", ok ? "ready" : "unavailable"));
    grid.append(row);
  });
}

function renderSummary(summary) {
  const { status, services, counts, upcoming_tasks: tasks, recent_activity: activity, recent_calls: calls } = summary;

  $("status-dot").className = "dot " + (status.paused ? "paused" : status.telegram_connected ? "online" : "offline");
  $("paused-pill").textContent = status.paused ? "Autonomy paused" : "Autonomy active";
  $("paused-pill").className = "pill " + (status.paused ? "paused" : "live");
  $("agent-state").textContent = status.telegram_connected ? "Online" : "Local only";
  $("agent-detail").textContent =
    "Telegram " + (status.telegram_connected ? "connected" : "not connected") +
    " · OpenAI " + (status.openai_configured ? "configured" : "missing key") +
    " · Scheduler " + (status.scheduler_running ? "running" : "stopped");

  $("tasks-pending").textContent = counts.tasks_pending;
  $("tasks-total").textContent = counts.tasks_total + " task(s) stored";
  $("reminders-pending").textContent = counts.reminders_pending;
  $("memory-total").textContent = summary.memory_total;
  $("conversations-total").textContent = counts.conversations + " conversation(s) · " + counts.calls + " call(s)";

  const taskList = $("task-list");
  taskList.replaceChildren();
  if (!tasks.length) taskList.append(el("li", "muted", "No tasks yet. Ask Ksenus to create one."));
  tasks.forEach((task) => {
    const item = el("li", "", task.title);
    item.append(el("span", "meta", "#" + task.id + " · " + task.priority + " · " + fmtDate(task.due_at) + " · " + task.status));
    taskList.append(item);
  });

  const feed = $("activity-list");
  feed.replaceChildren();
  if (!activity.length) feed.append(el("li", "muted", "No activity yet."));
  activity.forEach((event) => {
    const item = el("li", event.level === "ERROR" ? "error" : event.level === "WARNING" ? "warn" : "", event.message);
    item.append(el("span", "meta", event.source + " · " + fmtDate(event.at)));
    feed.append(item);
  });

  const callList = $("call-list");
  callList.replaceChildren();
  if (!calls.length) callList.append(el("li", "muted", "No calls recorded."));
  calls.forEach((call) => {
    const item = el("li", "", call.direction + " call · " + call.state);
    item.append(el("span", "meta", fmtDate(call.started_at) + (call.reason ? " — " + call.reason : "")));
    callList.append(item);
  });

  renderServices(services);
  $("stop-button").disabled = status.paused;
  $("resume-button").disabled = !status.paused;
}

async function refresh() {
  try {
    const summary = await api("/dashboard/summary");
    renderSummary(summary);
  } catch (error) {
    $("agent-detail").textContent = "Dashboard error: " + error.message;
  }
}

function appendMessage(role, text) {
  const log = $("chat-log");
  log.append(el("div", "msg " + role, text));
  log.scrollTop = log.scrollHeight;
}

async function sendChat(event) {
  event.preventDefault();
  const input = $("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  appendMessage("user", message);
  $("chat-status").textContent = "thinking…";
  try {
    const body = { message };
    if (state.conversationId) body.conversation_id = state.conversationId;
    const reply = await api("/chat", { method: "POST", body: JSON.stringify(body) });
    state.conversationId = reply.conversation_id;
    appendMessage("agent", reply.reply);

    const confirmation = $("confirmation");
    if (reply.confirmation) {
      state.confirmationId = reply.confirmation.id;
      $("confirmation-text").textContent =
        "Approval needed: " + reply.confirmation.tool + " — " + reply.confirmation.summary;
      confirmation.classList.remove("hidden");
    } else {
      state.confirmationId = null;
      confirmation.classList.add("hidden");
    }
    await refresh();
  } catch (error) {
    appendMessage("error", error.message);
  } finally {
    $("chat-status").textContent = "";
  }
}

async function approve() {
  if (!state.confirmationId) return;
  try {
    const result = await api("/confirmations/" + state.confirmationId, { method: "POST" });
    appendMessage("agent", "Confirmed: " + result.detail);
  } catch (error) {
    appendMessage("error", error.message);
  } finally {
    state.confirmationId = null;
    $("confirmation").classList.add("hidden");
    await refresh();
  }
}

async function setPaused(paused) {
  const path = paused ? "/control/stop" : "/control/resume";
  try {
    await api(path, { method: "POST" });
  } catch (error) {
    appendMessage("error", error.message);
  }
  await refresh();
}

document.addEventListener("DOMContentLoaded", () => {
  state.token = apiToken();

  $("chat-form").addEventListener("submit", sendChat);
  $("confirm-button").addEventListener("click", approve);
  $("stop-button").addEventListener("click", () => setPaused(true));
  $("resume-button").addEventListener("click", () => setPaused(false));

  refresh();
  window.setInterval(refresh, 15000);
});
