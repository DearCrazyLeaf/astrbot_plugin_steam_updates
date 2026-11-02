(() => {
  "use strict";

  const api = window.AstrBotPluginView;
  const state = { settings: null, dashboard: null, dirty: false };
  const byId = (id) => document.getElementById(id);

  async function get(path) {
    if (!api) throw new Error("AstrBot Plugin Page Bridge 尚未就绪");
    return api.apiGet(path);
  }

  async function post(path, body) {
    if (!api) throw new Error("AstrBot Plugin Page Bridge 尚未就绪");
    return api.apiPost(path, body);
  }

  function notify(message, type = "info") {
    const node = byId("message");
    node.textContent = message || "";
    node.className = message ? `message ${type}` : "message";
    if (message) window.setTimeout(() => { if (node.textContent === message) node.textContent = ""; }, 6000);
  }

  function escapeText(value) {
    return String(value ?? "");
  }

  function setPage(name) {
    const home = name === "home";
    byId("home-page").classList.toggle("hidden", !home);
    byId("settings-page").classList.toggle("hidden", home);
    byId("page-title").textContent = home ? "运行概览" : "插件配置";
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === name));
  }

  function fmtDate(date) {
    const parsed = new Date(`${date}T12:00:00`);
    return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", weekday: "short" }).format(parsed);
  }

  function fmtDateTime(value) {
    if (!value) return "暂无记录";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(parsed);
  }

  function renderDashboard(data) {
    state.dashboard = data;
    const status = data.runtime?.status || "waiting";
    const statusText = { waiting: "等待首次轮询", running: "轮询中", success: "运行中", failed: "最近轮询失败" }[status] || "状态未知";
    byId("status-pill").textContent = data.enabled ? statusText : "插件已停用";
    byId("status-pill").className = `status-pill ${data.enabled ? status : "off"}`;
    byId("hero-caption").textContent = !data.enabled
      ? "插件当前已停用；启用后即可恢复游戏动态监控。"
      : status === "running"
        ? "正在检查订阅源，稍后即可查看最新状态。"
        : status === "failed"
          ? "最近一次巡检遇到问题；请查看插件日志了解详情。"
          : data.runtime?.last_finished
            ? `最近巡检于 ${fmtDateTime(data.runtime.last_finished)} 完成，订阅动态持续跟进中。`
            : "订阅概况、轮询状态与限时免费游戏，一览即知。";
    byId("hero").classList.toggle("is-running", data.enabled && status === "running");
    byId("hero").classList.toggle("is-failed", data.enabled && status === "failed");
    byId("hero").classList.toggle("is-off", !data.enabled);
    byId("hero-mode").textContent = !data.enabled
      ? "COMMUNITY SIGNAL · MONITOR PAUSED"
      : status === "running"
        ? "COMMUNITY SIGNAL · CHECK IN PROGRESS"
        : status === "failed"
          ? "COMMUNITY SIGNAL · ATTENTION REQUIRED"
          : "COMMUNITY SIGNAL · LIVE MONITOR";

    const metrics = [
      { title: "游戏订阅", value: data.global.game_count, detail: "全局默认来源", code: "01", tone: "blue" },
      { title: "工坊订阅", value: data.global.workshop_count, detail: data.global.workshop_enabled ? "监控已开启" : "监控已关闭", code: "02", tone: "violet" },
      { title: "会话规则", value: data.session_count, detail: `${data.enabled_session_count} 个规则正在运行`, code: "03", tone: "green" },
      { title: "免费活动", value: data.global.free_games_enabled ? "开启" : "关闭", detail: `网络模式 · ${data.global.proxy_mode || "system"}`, code: "04", tone: "orange" },
    ];
    byId("metric-grid").replaceChildren(...metrics.map(({ title, value, detail, code, tone }) => {
      const card = document.createElement("article");
      card.className = `metric-card metric-${tone}`;
      const head = document.createElement("div");
      head.className = "metric-head";
      const label = document.createElement("span");
      label.className = "metric-label";
      label.textContent = title;
      const number = document.createElement("span");
      number.className = "metric-code";
      number.textContent = code;
      head.append(label, number);
      const valueNode = document.createElement("strong");
      valueNode.className = "metric-value";
      valueNode.textContent = String(value);
      const visual = document.createElement("div");
      visual.className = "metric-visual";
      visual.setAttribute("aria-hidden", "true");
      [34, 52, 41, 68, 47, 76, 57, 88, 63, 100].forEach((height, index) => {
        const bar = document.createElement("i");
        bar.style.setProperty("--bar-height", `${height}%`);
        bar.style.opacity = String(0.33 + index * 0.06);
        visual.append(bar);
      });
      const hint = document.createElement("small");
      hint.className = "metric-detail";
      hint.textContent = detail;
      card.append(head, valueNode, visual, hint);
      return card;
    }));

    const overview = byId("subscription-overview");
    overview.replaceChildren();
    const scopeCard = document.createElement("article");
    scopeCard.className = "scope-card";
    const scopeCopy = document.createElement("div");
    scopeCopy.className = "scope-copy";
    const scopeKicker = document.createElement("span");
    scopeKicker.className = "scope-kicker";
    scopeKicker.textContent = data.uses_global ? "GLOBAL ROUTING" : "SESSION ROUTING";
    const scopeTitle = document.createElement("strong");
    scopeTitle.textContent = data.uses_global ? "全局订阅正在生效" : "按会话独立分发";
    const scopeDescription = document.createElement("p");
    scopeDescription.textContent = data.uses_global
      ? "尚未配置分会话规则，所有推送目标使用统一的全局订阅。"
      : "已配置分会话规则；每个目标根据自己的订阅卡片接收动态。";
    scopeCopy.append(scopeKicker, scopeTitle, scopeDescription);
    const scopeStats = document.createElement("div");
    scopeStats.className = "scope-stats";
    [
      ["游戏", data.global.game_count, "项全局默认"],
      ["工坊", data.global.workshop_count, data.global.workshop_enabled ? "监控开启" : "监控关闭"],
    ].forEach(([label, count, detail]) => {
      const stat = document.createElement("div");
      stat.className = "scope-stat";
      const statLabel = document.createElement("small");
      statLabel.textContent = label;
      const statCount = document.createElement("strong");
      statCount.textContent = String(count);
      const statDetail = document.createElement("span");
      statDetail.textContent = detail;
      stat.append(statLabel, statCount, statDetail);
      scopeStats.append(stat);
    });
    scopeCard.append(scopeCopy, scopeStats);
    overview.append(scopeCard);

    byId("session-summary").textContent = `${data.session_count} 个会话 · ${data.enabled_session_count} 个启用`;
    const sessions = data.sessions || [];
    byId("sessions").replaceChildren(...sessions.map((session, index) => {
      const card = document.createElement("details");
      card.className = `session-card${session.enabled ? " session-enabled" : " session-paused"}`;
      const summary = document.createElement("summary");
      const identity = document.createElement("span");
      identity.className = "session-identity";
      const avatar = document.createElement("span");
      avatar.className = "session-avatar";
      avatar.textContent = String(index + 1).padStart(2, "0");
      const name = document.createElement("strong");
      name.textContent = session.label || session.target;
      identity.append(avatar, name);
      const stateText = document.createElement("span");
      stateText.className = session.enabled ? "tag good session-state" : "tag session-state";
      stateText.textContent = session.enabled ? "运行中" : "已暂停";
      summary.append(identity, stateText);
      const details = document.createElement("div");
      details.className = "session-details";
      const rows = [
        ["目标", session.target, "target"],
        ["游戏", `${session.game_count} 项`, "games"],
        ["创意工坊", `${session.workshop_count} 项`, "workshop"],
        ["免费领取", { inherit: "继承全局", on: "开启", off: "关闭" }[session.free_games_mode] || session.free_games_mode, "free"],
        ["消息模式", { inherit: "继承全局", card: "图片卡片", text: "文字" }[session.message_mode] || session.message_mode, "mode"],
      ];
      rows.forEach(([label, value, kind]) => {
        const row = document.createElement("div");
        row.className = `session-detail session-detail-${kind}`;
        const rowLabel = document.createElement("small");
        rowLabel.textContent = label;
        const rowValue = document.createElement("strong");
        rowValue.textContent = value;
        row.append(rowLabel, rowValue);
        details.append(row);
      });
      if (!session.valid) {
        const warning = document.createElement("span");
        warning.className = "tag warn";
        warning.textContent = "配置待检查";
        details.append(warning);
      }
      card.append(summary, details);
      return card;
    }));
    if (!sessions.length) {
      const empty = document.createElement("p");
      empty.className = "session-empty";
      const mark = document.createElement("span");
      mark.className = "empty-mark";
      mark.textContent = "◎";
      const copy = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = "当前由全局配置统一管理";
      const hint = document.createElement("small");
      hint.textContent = "需要为不同群聊设置不同订阅时，可在配置页添加会话规则。";
      copy.append(title, hint);
      empty.append(mark, copy);
      byId("sessions").append(empty);
    }

    const calendar = data.calendar || [];
    const recordedDays = calendar.filter((day) => day.status !== "unknown").length;
    const freeCount = calendar.reduce((total, day) => total + (day.items?.length || 0), 0);
    byId("calendar-notice").textContent = `${data.free_games_enabled ? "仅记录成功查询时观测到的活动" : "免费游戏采集已关闭，已有记录可能不完整"} · 已记录状态 ${recordedDays}/7 天 · 累计 ${freeCount} 项活动记录`;
    byId("calendar").replaceChildren(...calendar.map((day) => {
      const card = document.createElement("article");
      card.className = `day-card ${day.status}`;
      const dayMarker = document.createElement("span");
      dayMarker.className = "day-marker";
      dayMarker.setAttribute("aria-hidden", "true");
      const date = document.createElement("strong");
      date.textContent = fmtDate(day.date);
      const iso = document.createElement("small");
      iso.textContent = day.date;
      const top = document.createElement("div");
      top.className = "day-top";
      top.append(dayMarker, date);
      const count = document.createElement("span");
      count.className = "day-count";
      count.textContent = day.status === "failed" ? "失败" : day.status === "unknown" ? "待采集" : `${day.items?.length || 0} 项`;
      card.append(top, iso, count);
      if (day.status === "failed") {
        const empty = document.createElement("p");
        empty.textContent = "查询失败，无法确认";
        card.append(empty);
      } else if (day.status === "unknown") {
        const empty = document.createElement("p");
        empty.textContent = "暂无记录";
        card.append(empty);
      } else if (!day.items?.length) {
        const empty = document.createElement("p");
        empty.textContent = "当天未发现限时免费游戏";
        card.append(empty);
      } else {
        day.items.slice(0, 3).forEach((item) => {
          const entry = document.createElement("a");
          entry.className = "game-entry";
          entry.textContent = item.title || "未知游戏";
          entry.href = item.url || "#";
          if (item.url) {
            entry.target = "_blank";
            entry.rel = "noopener noreferrer";
          }
          card.append(entry);
        });
        if (day.items.length > 4) {
          const more = document.createElement("small");
          more.textContent = `还有 ${day.items.length - 4} 项`;
          card.append(more);
        }
      }
      return card;
    }));

    const runtime = data.runtime || {};
    const pollingBadge = byId("polling-badge");
    pollingBadge.className = `polling-badge polling-${data.enabled ? status : "off"}`;
    pollingBadge.textContent = !data.enabled
      ? "插件已停用"
      : status === "running"
        ? "巡检进行中"
        : status === "failed"
          ? "最近巡检异常"
          : status === "success"
            ? "自动巡检已运行"
            : "等待首次巡检";
    const values = [
      ["最近开始", fmtDateTime(runtime.last_started), "STARTED", "started"],
      ["最近结束", fmtDateTime(runtime.last_finished), "COMPLETED", "finished"],
      ["下次计划", fmtDateTime(runtime.next_run), "NEXT CHECK", "next"],
    ];
    byId("runtime").replaceChildren(...values.map(([label, value, code, key]) => {
      const item = document.createElement("div");
      item.className = `runtime-item runtime-${key}`;
      const marker = document.createElement("span");
      marker.className = "runtime-marker";
      marker.setAttribute("aria-hidden", "true");
      const copy = document.createElement("div");
      copy.className = "runtime-copy";
      const title = document.createElement("small");
      title.textContent = label;
      const text = document.createElement("span");
      text.textContent = value;
      const reference = document.createElement("i");
      reference.textContent = code;
      copy.append(title, text, reference);
      item.append(marker, copy);
      return item;
    }));
    byId("polling-track").className = `polling-track track-${data.enabled ? status : "off"}`;
    byId("polling-track").innerHTML = '<span class="track-line"></span><span class="track-node"></span><span class="track-node"></span><span class="track-node"></span>';
  }

  function inputElement(group, key, field, value, index = null) {
    const wrap = document.createElement("label");
    wrap.className = "field";
    wrap.dataset.group = group;
    wrap.dataset.key = key;
    if (index !== null) wrap.dataset.index = String(index);
    if (field.fieldKey) wrap.dataset.fieldKey = field.fieldKey;
    const title = document.createElement("span");
    title.textContent = field.description || key;
    wrap.append(title);
    let input;
    if (field.type === "bool") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = value === true;
      input.dataset.type = "bool";
      wrap.classList.add("toggle-field");
    } else if (field.options) {
      input = document.createElement("select");
      field.options.forEach((option, optionIndex) => {
        const choice = document.createElement("option");
        choice.value = option;
        const labels = field.labels || [];
        choice.textContent = labels[optionIndex] || option;
        input.append(choice);
      });
      input.value = value ?? field.default ?? "";
    } else if (field.type === "list") {
      input = document.createElement("textarea");
      input.rows = 3;
      input.value = Array.isArray(value) ? value.join("\n") : "";
      input.dataset.type = "list";
      input.placeholder = "每行一项";
    } else if (field.type === "int" || field.type === "float" || field.type === "number") {
      input = document.createElement("input");
      input.type = "number";
      input.value = value ?? field.default ?? 0;
      input.step = field.type === "int" ? "1" : "any";
      input.dataset.type = field.type;
    } else if (field.type === "text" || (field.type === "string" && (key.includes("prompt") || key.includes("template")))) {
      input = document.createElement("textarea");
      input.rows = 5;
      input.value = value ?? field.default ?? "";
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.value = value ?? field.default ?? "";
    }
    if (field.type !== "bool") input.classList.add("control");
    input.addEventListener("input", () => { state.dirty = true; });
    input.addEventListener("change", () => { state.dirty = true; });
    wrap.append(input);
    if (field.hint) {
      const hint = document.createElement("small");
      hint.className = "field-hint";
      hint.textContent = field.hint;
      wrap.append(hint);
    }
    if (field.secret) {
      input.value = "";
      input.placeholder = field.configured ? "已配置；留空保持不变" : "未配置";
      const clearWrap = document.createElement("span");
      clearWrap.className = "clear-secret";
      const clear = document.createElement("input");
      clear.type = "checkbox";
      clear.dataset.clearSecret = key;
      const label = document.createElement("span");
      label.textContent = "清除已保存的值";
      clearWrap.append(clear, label);
      wrap.append(clearWrap);
    }
    return wrap;
  }

  function renderSettings(payload) {
    state.settings = payload;
    const host = byId("settings-groups");
    host.replaceChildren();
    Object.entries(payload.groups || {}).forEach(([groupKey, group]) => {
      const section = document.createElement("details");
      section.className = "settings-group";
      section.open = groupKey === "basic_settings";
      const summary = document.createElement("summary");
      summary.textContent = group.description;
      section.append(summary);
      if (group.hint) {
        const hint = document.createElement("p");
        hint.className = "muted";
        hint.textContent = group.hint;
        section.append(hint);
      }
      const fields = document.createElement("div");
      fields.className = "field-grid";
      const values = group.values || {};
      Object.entries(group.items || {}).forEach(([key, field]) => {
        if (field.invisible) return;
        if (field.type === "template_list") {
          const list = document.createElement("div");
          list.className = "session-editor";
          list.dataset.group = groupKey;
          list.dataset.key = key;
          list.dataset.templateList = "true";
          (values[key] || []).forEach((entry, index) => {
            const card = document.createElement("div");
            card.className = "session-edit-card";
            const title = document.createElement("div");
            title.className = "session-edit-heading";
            title.textContent = `群聊/会话 ${index + 1}`;
            card.append(title);
            const template = field.templates?.session?.items || {};
            Object.entries(template).forEach(([subKey, subField]) => {
              if (!subField.invisible) card.append(inputElement(groupKey, key, { ...subField, fieldKey: subKey }, entry[subKey], index));
            });
            list.append(card);
          });
          const add = document.createElement("button");
          add.type = "button";
          add.className = "secondary add-session";
          add.textContent = "添加群聊/会话";
          add.addEventListener("click", () => {
            const next = structuredClone(values[key] || []);
            next.push({ __template_key: "session", label: "", umo: "", enabled: true, steam_appids: [], workshop_item_ids: [], free_games_mode: "inherit", message_mode: "inherit", baseline_token: "" });
            values[key] = next;
            renderSettings(payload);
            state.dirty = true;
          });
          list.append(add);
          fields.append(list);
          return;
        }
        const fieldWithSecret = _SECRET_KEY_SET.has(key)
          ? { ...field, secret: true, configured: Boolean(payload.secrets?.[key]) }
          : field;
        fields.append(inputElement(groupKey, key, fieldWithSecret, values[key]));
      });
      section.append(fields);
      host.append(section);
    });
    host.querySelectorAll(".session-edit-card").forEach((card) => {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger-button remove-session";
      remove.textContent = "删除会话卡片";
      remove.addEventListener("click", () => {
        const cards = [...host.querySelectorAll(".session-edit-card")];
        const index = cards.indexOf(card);
        const values = payload.groups.subscription_settings.values.sessions || [];
        values.splice(index, 1);
        renderSettings(payload);
        state.dirty = true;
      });
      card.append(remove);
    });
    state.dirty = false;
  }

  const _SECRET_KEY_SET = new Set(["steam_web_api_key", "proxy_url"]);

  function collectSettings() {
    const groups = {};
    Object.entries(state.settings.groups).forEach(([group, data]) => {
      groups[group] = structuredClone(data.values || {});
    });
    document.querySelectorAll("#settings-groups .field").forEach((field) => {
      const { group, key, index } = field.dataset;
      if (!group || !key) return;
      const input = field.querySelector("input,textarea,select");
      if (!input) return;
      let value;
      const type = input.dataset.type;
      if (type === "bool") value = input.checked;
      else if (type === "list") value = input.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      else if (type === "int") value = Number.parseInt(input.value, 10);
      else if (type === "float" || type === "number") value = Number(input.value);
      else value = input.value;
      if (index !== undefined) {
        const listKey = key;
        const entries = groups[group][listKey] || [];
        const actualKey = field.dataset.fieldKey;
        if (actualKey) entries[Number(index)][actualKey] = value;
        groups[group][listKey] = entries;
      } else {
        groups[group][key] = value;
      }
    });
    const clearSecrets = [...document.querySelectorAll("[data-clear-secret]:checked")].map((item) => item.dataset.clearSecret);
    return { groups, clear_secrets: clearSecrets };
  }

  async function loadDashboard() {
    try {
      renderDashboard(await get("/dashboard"));
    } catch (error) {
      notify(`主页加载失败：${error.message}`, "error");
      byId("status-pill").textContent = "连接失败";
    }
  }

  async function loadSettings() {
    try {
      renderSettings(await get("/settings"));
    } catch (error) {
      notify(`配置加载失败（/settings）：${error.message}`, "error");
      return;
    }

    const results = await Promise.allSettled([loadGames(), loadCovers()]);
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        const endpointName = index === 0 ? "游戏名称（/games）" : "封面缓存（/covers）";
        notify(`${endpointName}加载失败：${result.reason.message}`, "error");
      }
    });
  }

  async function saveSettings() {
    try {
      await post("/settings", collectSettings());
      notify("配置已保存。", "success");
      state.dirty = false;
      await Promise.all([loadDashboard(), loadSettings()]);
    } catch (error) {
      notify(`保存失败：${error.message}`, "error");
    }
  }

  async function loadGames() {
    const data = await get("/games");
    const host = byId("games-list");
    host.replaceChildren();
    (data.items || []).forEach((game) => {
      const row = document.createElement("article");
      row.className = "game-row";
      const title = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = game.name;
      const id = document.createElement("small");
      id.textContent = `AppID ${game.appid}`;
      title.append(name, id);
      const translations = document.createElement("div");
      translations.className = "translations";
      Object.entries(game.manual_names || {}).forEach(([language, value]) => {
        const entry = document.createElement("span");
        entry.className = "translation-chip";
        entry.textContent = `${language}: ${value}`;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.title = "移除此语言的手工覆盖";
        remove.addEventListener("click", async () => {
          try {
            await post("/games", { appid: game.appid, language, delete: true });
            await loadGames();
            notify("手工名称覆盖已删除。", "success");
          } catch (error) { notify(error.message, "error"); }
        });
        entry.append(remove);
        translations.append(entry);
      });
      row.append(title, translations);
      host.append(row);
    });
  }

  async function loadCovers() {
    const data = await get("/covers");
    const host = byId("covers-list");
    coverObserver?.disconnect();
    host.replaceChildren();
    if (!data.items?.length) {
      const empty = document.createElement("p");
      empty.className = "muted empty";
      empty.textContent = "尚无缓存封面。";
      host.append(empty);
      return;
    }
    data.items.forEach((cover) => {
      const card = document.createElement("article");
      card.className = "cover-card";
      const image = document.createElement("img");
      image.loading = "lazy";
      image.alt = `AppID ${cover.appid} 的缓存封面`;
      image.dataset.appid = cover.appid;
      image.src = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 460 215'%3E%3Crect width='460' height='215' fill='%230c151d'/%3E%3Cpath d='M214 94h32v27h-32zM223 83h14v11h-14z' fill='%233b5d73'/%3E%3C/svg%3E";
      image.addEventListener("error", () => {
        image.classList.add("image-unavailable");
        image.alt = `AppID ${cover.appid} 的封面暂时无法显示`;
      }, { once: true });
      const meta = document.createElement("div");
      const id = document.createElement("strong");
      id.textContent = `AppID ${cover.appid}`;
      const size = document.createElement("small");
      size.textContent = `${Math.ceil(cover.size / 1024)} KB`;
      meta.append(id, size);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger-button";
      remove.textContent = "删除";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`删除 AppID ${cover.appid} 的封面缓存？`)) return;
        try {
          await post(`/covers/${encodeURIComponent(cover.appid)}/delete`, {});
          await loadCovers();
          notify("封面缓存已删除。", "success");
        } catch (error) { notify(error.message, "error"); }
      });
      card.append(image, meta, remove);
      host.append(card);
      if (coverObserver) coverObserver.observe(image);
      else loadCoverImage(image);
    });
  }

  async function loadCoverImage(image) {
    if (image.dataset.loading === "true" || image.dataset.loaded === "true") return;
    image.dataset.loading = "true";
    try {
      const cover = await get(`/covers/${encodeURIComponent(image.dataset.appid)}`);
      if (cover.content_type !== "image/jpeg" || typeof cover.base64 !== "string") {
        throw new Error("封面响应格式无效");
      }
      image.src = `data:${cover.content_type};base64,${cover.base64}`;
      image.dataset.loaded = "true";
    } catch (error) {
      image.classList.add("image-unavailable");
      image.alt = `AppID ${image.dataset.appid} 的封面加载失败`;
      const hint = document.createElement("small");
      hint.className = "cover-load-error";
      hint.textContent = "图片加载失败，可刷新后重试";
      image.closest(".cover-card")?.append(hint);
      image.dataset.loadError = error.message;
    } finally {
      image.dataset.loading = "false";
    }
  }

  const coverObserver = "IntersectionObserver" in window
    ? new IntersectionObserver((entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        loadCoverImage(entry.target);
      });
    }, { rootMargin: "160px" })
    : null;

  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => setPage(button.dataset.page)));
  document.querySelectorAll("[data-open-settings]").forEach((button) => button.addEventListener("click", () => setPage("settings")));
  byId("save-settings").addEventListener("click", saveSettings);
  byId("reload-settings").addEventListener("click", () => {
    if (state.dirty && !window.confirm("放弃未保存的更改并重新载入？")) return;
    loadSettings();
  });
  byId("reload-covers").addEventListener("click", () => loadCovers().catch((error) => notify(error.message, "error")));
  byId("game-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await post("/games", {
        appid: form.get("appid"),
        language: form.get("language"),
        name: form.get("name"),
      });
      event.currentTarget.reset();
      await loadGames();
      notify("游戏名称已保存。", "success");
    } catch (error) { notify(error.message, "error"); }
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) { event.preventDefault(); event.returnValue = ""; }
  });

  if (api?.ready) {
    api.ready().then(() => Promise.all([loadDashboard(), loadSettings()]));
  } else {
    Promise.all([loadDashboard(), loadSettings()]);
  }
})();
