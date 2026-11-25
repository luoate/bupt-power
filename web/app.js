(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var toastTimer = null;
  var countdown = null;
  var checkCountdown = null;

  function toast(msg, isError) {
    var el = $("toast");
    el.textContent = msg;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast"; }, 3200);
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: "响应格式异常" }; })
        .then(function (body) {
          if (!r.ok || body.ok === false) {
            throw new Error(body.error || ("请求失败 " + r.status));
          }
          return body;
        });
    });
  }

  /* ---------- 读数 ---------- */

  function fmt(n, digits) {
    var v = parseFloat(n);
    if (!isFinite(v)) return "—";
    return v.toFixed(digits == null ? 2 : digits);
  }

  function paint(data) {
    $("roomLabel").textContent = data.room || "—";

    var balance = parseFloat(data.balance);
    var threshold = parseFloat(data.threshold);
    var days = parseFloat(data.preUseDay);
    var offline = data.onlineStatus && data.onlineStatus !== "在线";
    var low = isFinite(threshold) && isFinite(balance) && balance < threshold;
    var shortRunway = isFinite(days) && days < 3;

    $("balanceNum").textContent = fmt(data.balance);
    $("readout").className = "readout" + (low ? " is-low" : shortRunway ? " is-warn" : "");

    var badge = $("stateBadge");
    badge.hidden = false;
    if (offline) {
      badge.textContent = "离线";
      badge.className = "badge off";
    } else if (low) {
      badge.textContent = "低于阈值";
      badge.className = "badge low";
    } else if (shortRunway) {
      badge.textContent = "电量将尽";
      badge.className = "badge warn";
    } else {
      badge.textContent = "正常";
      badge.className = "badge ok";
    }

    $("stamp").textContent = "数据更新于 " + (data.updateDateTime || "—") +
      (data.meterStatus ? " · " + data.meterStatus.replace(/;$/, "") : "");

    var pct = isFinite(days) ? Math.max(0, Math.min(days / 14, 1)) * 100 : 0;
    $("runwayFill").style.width = pct + "%";
    $("runwayText").textContent = isFinite(days)
      ? "剩余约 " + fmt(days, 1) + " 天用量"
      : "暂无预估";
    $("thresholdText").textContent = isFinite(threshold) ? "告警线 " + fmt(threshold) + " 元" : "";

    $("sMeter").textContent = fmt(data.meterData, 2);
    $("sRemain").textContent = fmt(data.remaining, 2);
    $("sDays").textContent = isFinite(days) ? fmt(days, 1) : "—";

    var quota = data.quota;
    $("quotaText").textContent = (typeof quota === "number")
      ? "今日剩余 " + quota + " / 10 次"
      : "今日额度 —";

    startCountdown(data.nextRefreshIn);
    startCheckCountdown(data.nextCheckIn);
    renderScheduler(data.scheduler);
    renderSpark(data.history || []);
    renderTable(data.history || []);
  }

  function startCheckCountdown(seconds) {
    clearInterval(checkCountdown);
    var el = $("checkText");
    if (seconds === null || seconds === undefined) {
      el.textContent = "";
      return;
    }
    var left = Math.max(0, seconds);
    var tick = function () {
      if (left <= 0) {
        el.textContent = "即将读取余额";
        clearInterval(checkCountdown);
        return;
      }
      var m = Math.floor(left / 60);
      var s = left % 60;
      el.textContent = "下次读余额 " + (m ? m + " 分 " : "") + s + " 秒后";
      left -= 1;
    };
    tick();
    checkCountdown = setInterval(tick, 1000);
  }

  function renderScheduler(s) {
    var el = $("schedNote");
    if (!s || !s.running) {
      el.textContent = "后台监控未启动";
      return;
    }
    if (s.lastRunAt) {
      var went = s.lastStatus === "error" ? "失败：" + (s.lastMessage || "")
        : (s.lastMessage || "已完成");
      el.textContent = "后台监控运行中 · 上次 " + shortTime(s.lastRunAt) + " " + went;
    } else {
      el.textContent = "后台监控运行中 · 尚未到达检查时间";
    }
  }

  function startCountdown(seconds) {
    clearInterval(countdown);
    var el = $("autoText");
    if (seconds === null || seconds === undefined) {
      el.textContent = "自动抄表已关闭";
      return;
    }
    var left = Math.max(0, seconds);
    var tick = function () {
      if (left <= 0) {
        el.textContent = "等待下次自动抄表";
        clearInterval(countdown);
        return;
      }
      var h = Math.floor(left / 3600);
      var m = Math.floor((left % 3600) / 60);
      var s = left % 60;
      el.textContent = "下次自动抄表 " + (h ? h + " 时 " : "") + m + " 分 " + s + " 秒后";
      left -= 1;
    };
    tick();
    countdown = setInterval(tick, 1000);
  }

  /* ---------- 走势图 ---------- */

  function renderSpark(history) {
    var box = $("spark");
    box.textContent = "";
    var pts = history.slice().reverse()
      .map(function (r) { return parseFloat(r["余额(元)"]); })
      .filter(function (v) { return isFinite(v); });

    if (pts.length < 2) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = pts.length === 1
        ? "已有 1 条记录，再积累几条就能看到走势"
        : "暂无记录，刷新一次表计即可开始积累";
      box.appendChild(empty);
      $("sparkMeta").textContent = "";
      return;
    }

    var W = 700, H = 84, pad = 4;
    var min = Math.min.apply(null, pts);
    var max = Math.max.apply(null, pts);
    if (max === min) { max = min + 1; }

    var step = (W - pad * 2) / (pts.length - 1);
    var coords = pts.map(function (v, i) {
      var x = pad + i * step;
      var y = H - pad - ((v - min) / (max - min)) * (H - pad * 2);
      return [x, y];
    });

    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("preserveAspectRatio", "none");

    var area = document.createElementNS(ns, "path");
    area.setAttribute("d",
      "M " + coords[0][0] + " " + (H - pad) + " L " +
      coords.map(function (c) { return c[0] + " " + c[1]; }).join(" L ") +
      " L " + coords[coords.length - 1][0] + " " + (H - pad) + " Z");
    area.setAttribute("fill", "var(--accent-soft)");
    svg.appendChild(area);

    var line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", coords.map(function (c) { return c.join(","); }).join(" "));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "var(--accent)");
    line.setAttribute("stroke-width", "1.6");
    line.setAttribute("stroke-linejoin", "round");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(line);

    var last = coords[coords.length - 1];
    var dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", last[0]);
    dot.setAttribute("cy", last[1]);
    dot.setAttribute("r", "2.6");
    dot.setAttribute("fill", "var(--accent)");
    svg.appendChild(dot);

    box.appendChild(svg);
    $("sparkMeta").textContent = "近 " + pts.length + " 次 · 最低 " + fmt(min) + " 元";
  }

  function shortTime(s) {
    // "2026-11-25 11:46:06" -> "11-25 11:46"
    if (!s || s.length < 16) return s || "—";
    return s.slice(5, 16);
  }

  function renderTable(history) {
    var tbody = $("histTable").querySelector("tbody");
    tbody.textContent = "";
    history.slice(0, 40).forEach(function (r) {
      var tr = document.createElement("tr");
      [shortTime(r["查询时间"]), r["余额(元)"], r["表读数"], r["数据来源"] || "缓存"].forEach(function (v) {
        var td = document.createElement("td");
        td.textContent = v || "—";
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }

  /* ---------- 数据加载 ---------- */

  function loadStatus() {
    return api("/api/status")
      .then(paint)
      .catch(function (e) {
        $("stamp").textContent = "读取失败：" + e.message;
        $("balanceNum").textContent = "—";
        toast(e.message, true);
      });
  }

  function doRefresh() {
    var btn = $("btnRefresh");
    btn.disabled = true;
    btn.textContent = "抄表中…";
    api("/api/refresh", { method: "POST" })
      .then(function (data) {
        paint(data);
        toast("抄表成功，余额 " + fmt(data.balance) + " 元");
      })
      .catch(function (e) {
        toast(e.message, true);
        return loadStatus();
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = "刷新表计";
      });
  }

  /* ---------- 配置 ---------- */

  function loadConfig() {
    return api("/api/config").then(function (c) {
      $("threshold").value = c.threshold_yuan != null ? c.threshold_yuan : "";
      var r = c.refresh || {};
      $("interval").value = r.interval_hours != null ? r.interval_hours : 12;
      $("autoRefresh").checked = !!r.enabled;
      $("checkInterval").value = c.check_interval_minutes != null ? c.check_interval_minutes : 60;
      $("cooldown").value = (c.alert || {}).cooldown_hours != null ? c.alert.cooldown_hours : 24;
      $("roomLine").textContent = (c.room || {}).label || "—";

      var s = c.smtp || {};
      $("smtpHost").value = s.host || "";
      $("smtpPort").value = s.port || 465;
      $("smtpUser").value = s.username || "";
      $("recipients").value = (s.recipients || []).join(", ");
      $("smtpPass").value = "";
      $("passState").textContent = s.has_password
        ? "已保存授权码，留空表示不修改。"
        : "尚未设置授权码，邮件通知无法发送。";
    }).catch(function (e) { toast("读取配置失败：" + e.message, true); });
  }

  function formPayload() {
    var payload = {
      threshold_yuan: parseFloat($("threshold").value),
      check_interval_minutes: parseFloat($("checkInterval").value),
      alert: { cooldown_hours: parseFloat($("cooldown").value) },
      refresh: {
        enabled: $("autoRefresh").checked,
        interval_hours: parseFloat($("interval").value)
      },
      smtp: {
        host: $("smtpHost").value.trim(),
        port: parseInt($("smtpPort").value, 10) || 465,
        ssl: true,
        username: $("smtpUser").value.trim(),
        sender: $("smtpUser").value.trim(),
        password: $("smtpPass").value,
        recipients: $("recipients").value
      }
    };
    return payload;
  }

  function saveConfig(ev) {
    ev.preventDefault();

    var threshold = parseFloat($("threshold").value);
    if (!isFinite(threshold) || threshold <= 0) {
      toast("请填写大于 0 的告警阈值", true);
      $("threshold").focus();
      return;
    }
    var hours = parseFloat($("interval").value);
    if (!isFinite(hours) || hours < 1 || hours > 24) {
      toast("自动抄表间隔需在 1 到 24 小时之间", true);
      $("interval").focus();
      return;
    }
    var checkMin = parseFloat($("checkInterval").value);
    if (!isFinite(checkMin) || checkMin < 10 || checkMin > 1440) {
      toast("读取余额间隔需在 10 到 1440 分钟之间", true);
      $("checkInterval").focus();
      return;
    }
    var cooldown = parseFloat($("cooldown").value);
    if (!isFinite(cooldown) || cooldown < 1 || cooldown > 720) {
      toast("邮件冷却需在 1 到 720 小时之间", true);
      $("cooldown").focus();
      return;
    }

    var btn = $("btnSave");
    btn.disabled = true;
    api("/api/config", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(formPayload())
    }).then(function () {
      toast("设置已保存");
      return Promise.all([loadConfig(), loadStatus()]);
    }).catch(function (e) {
      toast(e.message, true);
    }).then(function () { btn.disabled = false; });
  }

  function testEmail() {
    var btn = $("btnTest");
    btn.disabled = true;
    btn.textContent = "发送中…";
    api("/api/test-email", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ smtp: formPayload().smtp })
    }).then(function () {
      toast("测试邮件已发送，请查收");
    }).catch(function (e) {
      toast(e.message, true);
    }).then(function () {
      btn.disabled = false;
      btn.textContent = "发送测试邮件";
    });
  }

  /* ---------- 宿舍选择 ---------- */

  var ROOT_ID = "001";
  var path = [{ id: ROOT_ID, name: "选择校区" }];
  var picked = null;   // { id, name, label }

  function renderCrumbs() {
    var box = $("crumbs");
    box.textContent = "";
    path.forEach(function (node, i) {
      if (i > 0) {
        var sep = document.createElement("span");
        sep.className = "sep";
        sep.textContent = "/";
        box.appendChild(sep);
      }
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = node.name;
      b.addEventListener("click", function () {
        path = path.slice(0, i + 1);
        picked = null;
        $("btnUseRoom").disabled = true;
        loadLevel(node.id);
      });
      box.appendChild(b);
    });
  }

  function roomLabelFromPath() {
    // 丢掉「3层」这类层级名，保留 校区/园区/楼/房间
    return path.slice(1)
      .map(function (n) { return n.name; })
      .filter(function (n) { return !/层$/.test(n); })
      .join(" ");
  }

  function loadLevel(id) {
    renderCrumbs();
    var box = $("choices");
    box.textContent = "";
    var loading = document.createElement("span");
    loading.className = "loading";
    loading.textContent = "读取中…";
    box.appendChild(loading);

    api("/api/buildings?parent=" + encodeURIComponent(id)).then(function (d) {
      box.textContent = "";
      if (!d.items.length) {
        var none = document.createElement("span");
        none.className = "none";
        none.textContent = "这一层没有可选项";
        box.appendChild(none);
        return;
      }
      d.items.forEach(function (item) {
        var b = document.createElement("button");
        b.type = "button";
        b.textContent = item.name;
        if (picked && picked.id === item.id) b.className = "on";
        b.addEventListener("click", function () { enterNode(item); });
        box.appendChild(b);
      });
      $("pickerHint").textContent = "逐级点选到房间";
    }).catch(function (e) {
      box.textContent = "";
      var err = document.createElement("span");
      err.className = "none";
      err.textContent = "读取失败：" + e.message;
      box.appendChild(err);
    });
  }

  function enterNode(item) {
    // 房间编号固定 24 位，是最后一层
    if (item.id && item.id.length === 24) {
      path.push(item);
      picked = { id: item.id, name: item.name, label: roomLabelFromPath() };
      renderCrumbs();
      var box = $("choices");
      Array.prototype.forEach.call(box.children, function (b) {
        b.className = b.textContent === item.name ? "on" : "";
      });
      $("btnUseRoom").disabled = false;
      $("pickerHint").textContent = "将使用：" + picked.label;
      return;
    }
    path.push(item);
    picked = null;
    $("btnUseRoom").disabled = true;
    loadLevel(item.id);
  }

  function openPicker() {
    var picker = $("picker");
    picker.hidden = false;
    if (!path.length) path = [{ id: ROOT_ID, name: "选择校区" }];
    loadLevel(path[path.length - 1].id);
  }

  function useRoom() {
    if (!picked) return;
    var btn = $("btnUseRoom");
    btn.disabled = true;
    api("/api/config", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ room: { building_id: picked.id, label: picked.label } })
    }).then(function () {
      $("roomLine").textContent = picked.label;
      $("picker").hidden = true;
      picked = null;
      btn.disabled = true;
      toast("已切换宿舍");
      return loadStatus();
    }).catch(function (e) {
      toast(e.message, true);
      btn.disabled = false;
    });
  }

  /* ---------- 启动 ---------- */

  $("btnRefresh").addEventListener("click", doRefresh);
  $("btnPickRoom").addEventListener("click", openPicker);
  $("btnUseRoom").addEventListener("click", useRoom);
  $("cfgForm").addEventListener("submit", saveConfig);
  $("btnTest").addEventListener("click", testEmail);

  loadStatus();
  loadConfig();
})();
