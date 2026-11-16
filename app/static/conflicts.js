/* conflicts.js —— 冲突组裁定页：选中组、渲染原证据与决定、提交采纳 / 撤销。 */
(function () {
  "use strict";

  var dataNode = document.getElementById("conflict-data");
  var groups = [];
  if (dataNode) {
    try {
      groups = JSON.parse(dataNode.textContent || "[]");
    } catch (err) {
      groups = [];
    }
  }
  var current = null;
  var selectedLine = null;
  var selectedState = { group: null, version: 0 };
  var actorInput = document.getElementById("actor-input");
  var reasonInput = document.getElementById("reason-input");
  var msg = document.getElementById("decision-msg");
  var detail = document.getElementById("conflict-detail");
  var detailMeta = document.getElementById("detail-meta");
  var adoptBtn = document.getElementById("adopt-btn");
  var revokeBtn = document.getElementById("revoke-btn");

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function money(value) {
    var number = Number(value);
    return isNaN(number) ? value : number.toLocaleString("zh-CN", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  function findGroup(id) {
    for (var index = 0; index < groups.length; index += 1) {
      if (groups[index].group_id === id) {
        return groups[index];
      }
    }
    return null;
  }

  function replaceGroup(group) {
    for (var index = 0; index < groups.length; index += 1) {
      if (groups[index].group_id === group.group_id) {
        groups[index] = group;
        return;
      }
    }
    groups.push(group);
  }

  function setMessage(text, isError) {
    if (!msg) { return; }
    msg.textContent = text;
    msg.className = isError ? "msg err" : "msg";
  }

  function renderDetail() {
    if (!current || !detail) { return; }
    var adoptedLine = current.adopted_line;
    var rows = current.snapshots.map(function (snap) {
      var checked = adoptedLine === snap.line_no ? " checked" : "";
      var match = adoptedLine === snap.line_no;
      return '<tr class="snap-row' + (match ? ' is-adopted' : '') + '" data-line="' + snap.line_no + '">' +
        '<td><input type="radio" name="snap" value="' + snap.line_no + '"' + checked + '></td>' +
        '<td class="mono">' + esc(snap.line_no) + "</td>" +
        '<td class="mono">' + esc(snap.snapshot_utc) + "</td>" +
        '<td class="mono">' + esc(snap.hotel_code) + "/" + esc(snap.channel) + "</td>" +
        '<td class="mono">' + esc(snap.rooms_available) + "</td>" +
        '<td class="mono">' + esc(snap.rooms_sold) + "</td>" +
        '<td class="num">' + money(snap.adr) + "</td>" +
        '<td class="num">' + money(snap.revenue_net) + "</td>" +
        '<td class="mono truncate" title="' + esc(snap.raw) + '">' + esc(snap.raw) + "</td>" +
        "</tr>";
    }).join("");

    var history = current.history.length ? current.history.map(function (item) {
      var action = item.action === "adopt" ? "采纳" : "撤销";
      return "<tr><td class=\"num\">v" + esc(item.version) + "</td>" +
        '<td><span class="tag ' + (item.action === "adopt" ? "pass" : "candidate") + '">' + action + "</span></td>" +
        '<td class="mono">' + (item.line_no === null ? "—" : esc(item.line_no)) + "</td>" +
        "<td>" + esc(item.reason) + "</td>" +
        '<td class="mono">' + esc(item.actor) + "</td>" +
        '<td class="mono">' + esc(item.created_at) + "</td></tr>";
    }).join("") : '<tr><td colspan="6" class="empty">还没有裁决，整组保持隔离。</td></tr>';

    var stateTag = current.state === "adopted"
      ? '<span class="tag pass">已采信 行 ' + esc(current.adopted_line) + "</span>"
      : '<span class="tag candidate">整组隔离</span>';
    detail.innerHTML =
      '<div class="split">' +
        '<div>' +
          "<h3>原报事实 · 候选快照 <span class=\"meta\">" + esc(current.snapshots.length) + " 行 · 全部保留 Q_DUP_CONFLICT</span></h3>" +
          '<table class="snap-table"><thead><tr><th class="static">选</th><th class="static">行号</th>' +
          '<th class="static">快照时间</th><th class="static">门店/渠道</th>' +
          '<th class="num static">可售</th><th class="num static">已售</th>' +
          '<th class="num static">均价</th><th class="num static">净收入</th><th class="static">原始行</th></tr></thead>' +
          "<tbody>" + rows + "</tbody></table>" +
        "</div>" +
        '<div class="decision-side">' +
          "<h3>当前决定 " + stateTag + "</h3>" +
          '<dl class="kv"><dt>版本</dt><dd>v' + esc(current.version) + "</dd>" +
          "<dt>状态</dt><dd>" + (current.state === "adopted" ? "裁定采信参与计算" : "隔离，不参与计算") + "</dd>" +
          "<dt>采信行</dt><dd>" + (current.adopted_line === null ? "—" : esc(current.adopted_line)) + "</dd>" +
          "<dt>最近决定</dt><dd>" + esc(current.last_decided_at || "—") + "</dd></dl>" +
          "<h3>历次裁决</h3>" +
          '<table><thead><tr><th class="num static">版本</th><th class="static">动作</th>' +
          '<th class="static">行</th><th class="static">理由</th><th class="static">裁决人</th>' +
          '<th class="static">时间</th></tr></thead><tbody>' + history + "</tbody></table>" +
        "</div>" +
      "</div>";

    selectedLine = current.adopted_line;
    Array.prototype.slice.call(detail.querySelectorAll("input[name='snap']"))
      .forEach(function (node) {
        node.addEventListener("change", function () {
          selectedLine = Number(node.value);
          syncButtons();
        });
      });
    detailMeta.textContent = current.hotel_code + "/" + current.channel + " · " + current.biz_date;
    syncButtons();
  }

  function syncButtons() {
    var hasGroup = !!current;
    if (adoptBtn) {
      adoptBtn.disabled = !hasGroup || selectedLine === null;
      adoptBtn.textContent = hasGroup && current.state === "adopted"
        ? "改采信所选快照" : "采纳所选快照";
    }
    if (revokeBtn) {
      revokeBtn.disabled = !hasGroup || current.state !== "adopted";
    }
  }

  function selectGroup(id) {
    current = findGroup(id);
    if (!current) { return; }
    selectedState.group = id;
    selectedState.version = current.version;
    setMessage("", false);
    Array.prototype.slice.call(document.querySelectorAll(".conflict-row"))
      .forEach(function (row) {
        row.classList.toggle("is-active", row.getAttribute("data-group") === id);
      });
    renderDetail();
  }

  function post(path, payload, done) {
    fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, status: response.status, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          var code = result.body.error || "失败";
          setMessage(result.status + " " + code + " " + (result.body.message || ""), true);
          if (result.body.error === "E_DECISION_STALE" && current) {
            refresh(current.group_id);
          }
          return;
        }
        done(result.body);
      })
      .catch(function (err) {
        setMessage("请求失败：" + err, true);
      });
  }

  function refresh(id) {
    fetch("/api/conflicts/" + encodeURIComponent(id))
      .then(function (response) { return response.json(); })
      .then(function (body) {
        if (body.group) {
          replaceGroup(body.group);
          selectGroup(body.group.group_id);
        }
      })
      .catch(function () {});
  }

  function requestId() {
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    return "req-" + Date.now() + "-" + Math.floor(Math.random() * 1e6);
  }

  function actorReason() {
    var actor = (actorInput && actorInput.value || "").trim();
    var reason = (reasonInput && reasonInput.value || "").trim();
    if (!actor) {
      setMessage("请填写裁决人", true);
      return null;
    }
    if (reason.length < 4) {
      setMessage("理由至少 4 个字", true);
      return null;
    }
    return { actor: actor, reason: reason };
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.slice.call(document.querySelectorAll(".conflict-row"))
      .forEach(function (row) {
        row.addEventListener("click", function () {
          selectGroup(row.getAttribute("data-group"));
        });
        row.addEventListener("keydown", function (event) {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectGroup(row.getAttribute("data-group"));
          }
        });
      });

    if (adoptBtn) {
      adoptBtn.addEventListener("click", function () {
        if (!current || selectedLine === null) {
          setMessage("先在左侧选择一行快照", true);
          return;
        }
        var base = actorReason();
        if (!base) { return; }
        post("/api/conflicts/" + encodeURIComponent(current.group_id) + "/adopt", {
          line_no: selectedLine, actor: base.actor, reason: base.reason,
          expected_version: current.version, request_id: requestId()
        }, function (body) {
          replaceGroup(body.group);
          selectGroup(body.group.group_id);
          setMessage("已采纳行 " + body.group.adopted_line + "（v" + body.group.version + "）", false);
          setTimeout(function () { window.location.reload(); }, 400);
        });
      });
    }

    if (revokeBtn) {
      revokeBtn.addEventListener("click", function () {
        if (!current) { return; }
        var base = actorReason();
        if (!base) { return; }
        post("/api/conflicts/" + encodeURIComponent(current.group_id) + "/revoke", {
          actor: base.actor, reason: base.reason,
          expected_version: current.version, request_id: requestId()
        }, function (body) {
          replaceGroup(body.group);
          selectGroup(body.group.group_id);
          setMessage("已撤销，整组恢复隔离（v" + body.group.version + "）", false);
          setTimeout(function () { window.location.reload(); }, 400);
        });
      });
    }
  });
})();
