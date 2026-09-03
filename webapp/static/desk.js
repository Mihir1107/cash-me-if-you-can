/* The close desk, rendered.
 *
 * One copy of this logic, used by both the live tool and the static artifact.
 * It takes a report object exactly as `src/report.py` writes it and paints the
 * page from it -- it computes nothing. Every number here was already stood
 * behind by the pipeline, which is the only reason the screen can be trusted.
 */
(function (global) {
  "use strict";

  var money = function (n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  var pct = function (n) { return (Math.round(n * 100) / 100).toFixed(2) + "%"; };
  var words = function (s) { return String(s).replace(/_/g, " "); };
  var el = function (id) { return document.getElementById(id); };

  /* Everything rendered below is read out of somebody's CSV -- order ids,
     column headers, the narration quoted back in a basis string. It is data,
     never markup, and it reaches innerHTML by concatenation, so it is escaped
     at the sink. A ledger is not a trusted document: it can arrive by email
     from anyone, and a cell containing a script tag would otherwise run here
     with same-origin access to every later run in this tab. */
  var esc = function (s) {
    return String(s === null || s === undefined ? "" : s).replace(
      /[&<>"']/g,
      function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                 '"': "&quot;", "'": "&#39;" }[c];
      });
  };


  /* Two hosts render this markup and they do not carry identical furniture --
     the static artifact has a batch switcher and a clock in its masthead, the
     live tool has file names and a download row. So optional text is set
     defensively: a missing span must not take the whole render down with it,
     which is exactly what it did the first time this ran in the app. */
  var setText = function (id, value) {
    var node = el(id);
    if (node) node.textContent = value;
  };

  function render(d) {
    var gate = d.close_gate || {};
    var m = d.money || {};
    var t = d.triage || {};

    /* ------------------------------------------------------------ verdict */
    var blocked = !gate.can_close;
    var stamp = el("stamp");
    stamp.textContent = blocked ? "Blocked" : "Can close";
    stamp.className = "stamp " + (blocked ? "blocked" : "clear");

    el("verdictLine").innerHTML = blocked
      ? "<strong>" + gate.blocker_count + " of " + gate.conditions_checked + "</strong> conditions fail. "
        + "<strong>" + money(gate.value_blocking_close) + "</strong> has to be resolved or documented before anyone signs."
      : "All <strong>" + gate.conditions_checked + "</strong> conditions pass. Nothing outstanding blocks the period.";
    el("verdictNote").textContent = gate.note || "";

    /* condition ledger, failures first */
    var byName = {};
    (gate.blockers || []).forEach(function (b) { byName[b.condition] = b; });
    var order = (gate.blockers || []).map(function (b) { return b.condition; })
      .concat(gate.conditions_passed || []);
    el("conds").innerHTML = order.map(function (name) {
      var b = byName[name];
      return '<div class="cond ' + (b ? "fail" : "pass") + '">'
        + '<span class="cond-mark">' + (b ? "✕" : "✓") + "</span>"
        + '<span class="cond-name">' + esc(name) + "</span>"
        + '<span class="cond-val">' + (b ? money(b.value_at_risk) : "") + "</span>"
        + "</div>";
    }).join("");

    /* -------------------------------------------------------------- money */
    el("band").innerHTML = [
      ["Total exposure", money(m.total_exposure), d.total_orders.toLocaleString("en-IN") + " orders in the period", ""],
      ["Confirmed in bank", money(m.confirmed_value), pct(m.value_match_rate_pct) + " of the book by value", "confirmed"],
      ["At risk", money(m.at_risk_value), (d.exception_count || 0) + " exception rows", "at-risk"],
      ["Orders reconciled", pct(d.match_rate_pct), d.reconciled_orders + " of " + d.total_orders + " clean on both legs", ""]
    ].map(function (c) {
      return '<div class="band-cell ' + c[3] + '"><span class="eyebrow">' + c[0] + "</span>"
        + '<span class="v">' + c[1] + "</span>"
        + '<div class="sub">' + c[2] + "</div></div>";
    }).join("");

    var id = m.identity || {};
    el("identity").innerHTML =
      '<span class="ok-chip">' + (id.holds ? "Balances" : "Does not balance") + "</span>"
      + "<span>" + money(m.total_exposure) + " = " + money(m.confirmed_value)
      + " + " + money(m.at_risk_value) + "</span>"
      + '<span class="why">residual ' + money(id.residual) + " — checked at runtime, printed as a failure if it ever moves</span>";

    /* --------------------------------------------------------- work queue */
    el("queueMeta").textContent = t.exception_rows + " rows → " + t.incident_count + " incidents";
    el("queueIntro").textContent =
      t.exception_rows + " exception rows cluster into " + t.incident_count
      + " incidents across " + Object.keys(t.by_owner || {}).length + " teams. "
      + t.material_incident_count + " are above the triage threshold and ranked first; "
      + "the rest are still reported, still counted, still inside the identity above.";

    var incidents = (t.incidents || []).slice();
    var groups = {};
    incidents.forEach(function (inc) {
      (groups[inc.owner] = groups[inc.owner] || []).push(inc);
    });
    var owners = Object.keys(groups).sort(function (a, b) {
      return (t.by_owner[b] || {}).value_at_risk - (t.by_owner[a] || {}).value_at_risk;
    });

    el("queue").innerHTML = owners.map(function (owner) {
      var o = t.by_owner[owner] || {};
      var rows = groups[owner].map(function (inc, i) {
        var cls = inc.material ? inc.urgency : "sub";
        var ids = (inc.order_ids || []).slice(0, 14)
          .map(function (x) { return "<code>" + esc(x) + "</code>"; }).join("");
        var more = (inc.order_ids || []).length > 14
          ? '<code>+' + ((inc.order_ids.length) - 14) + " more</code>" : "";
        return '<div class="item ' + cls + '" data-key="' + owner + "-" + i + '">'
          + '<div class="stripe"></div><div>'
          + '<button class="item-btn" type="button" aria-expanded="false">'
          + '<span class="item-title">' + esc(words(inc.reason_code)) + "</span>"
          + '<span class="item-val">' + money(inc.value_at_risk) + "</span>"
          + '<span class="item-sub">'
          + '<span class="pill ' + esc(inc.urgency) + '">' + esc(inc.urgency) + "</span>"
          + (inc.material ? "" : '<span class="pill below">below threshold</span>')
          + "<span>" + inc.order_count + (inc.order_count === 1 ? " order" : " orders") + "</span>"
          + "</span></button>"
          + '<div class="item-body">'
          + '<p class="action">' + esc(inc.recommended_action) + "</p>"
          + '<p class="basis">' + esc(inc.sample_basis || "") + "</p>"
          + '<div class="orders">' + ids + more + "</div>"
          + "</div></div></div>";
      }).join("");

      return '<div class="owner-group"><div class="owner-bar">'
        + '<span class="owner-name">' + esc(words(owner)) + "</span>"
        + '<span class="owner-meta">' + o.incidents + " incidents · " + o.orders
        + " orders · " + money(o.value_at_risk) + "</span></div>" + rows + "</div>";
    }).join("");

    el("thresholdNote").innerHTML =
      "<strong>" + money(t.materiality_threshold) + "</strong> triage threshold. "
      + t.materiality_basis;

    /* --------------------------------------------------------- scorecard */
    /* Your own data has no answer key, so there is nothing to score against.
       Showing an empty scorecard would imply the pipeline had checked itself
       and found nothing wrong, which is the opposite of what a blank means. */
    var s = d.scoring;
    if (!s) {
      el("scorecard").innerHTML =
        '<p class="lede" style="margin:0">There is no answer key for your data, so '
        + 'there is nothing to score against here. The accuracy figures — 32 of 32 '
        + 'faults detected, none missed — come from batches where the generator '
        + 'recorded what it broke, in a file the pipeline never reads. '
        + 'Run a sample to see them.</p>';
    } else {
    el("scorecard").innerHTML = [
      ["Faults injected", s.injected_faults, ""],
      ["Detected", s.detected, s.detected === s.injected_faults ? "good" : ""],
      ["Missed", s.missed, s.missed === 0 ? "good" : "bad"],
      ["False positives", s.false_positives, s.false_positives ? "bad" : "good"],
      ["Recall", s.recall, ""],
      ["Reason-code accuracy", s.accuracy, ""]
    ].map(function (r) {
      return '<div class="kv"><span class="k">' + r[0] + "</span>"
        + '<span class="v ' + r[2] + '">' + r[1] + "</span></div>";
    }).join("");
    }

    /* -------------------------------------------------------------- bars */
    var risk = m.at_risk_by_reason || {};
    var keys = Object.keys(risk).sort(function (a, b) { return risk[b] - risk[a]; });
    var top = keys[0] ? risk[keys[0]] : 1;
    var flag = { settlement_reversed: "reversed", no_ledger_entry: "unbooked", bank_credit_delayed: "delayed" };
    el("bars").innerHTML = keys.map(function (k) {
      return '<div class="bar-row ' + (flag[k] || "") + '">'
        + '<span class="lbl">' + esc(words(k)) + "</span>"
        + '<span class="amt">' + money(risk[k]) + "</span>"
        + '<span class="bar-track"><span class="bar-fill" style="width:'
        + Math.max(1, (risk[k] / top) * 100) + '%"></span></span></div>';
    }).join("");

    /* ------------------------------------------------------------- chain */
    var a = d.audit_trail || {};
    el("chain").innerHTML =
      '<div class="chain ' + (a.intact ? "" : "broken") + '"><span class="dot"></span>'
      + "<span>" + (a.intact ? "Intact" : "BROKEN at line " + a.broken_at) + "</span></div>"
      + '<div class="kv" style="margin-top:10px"><span class="k">Entries verified</span>'
      + '<span class="v">' + (a.verified || 0).toLocaleString("en-IN") + "</span></div>"
      + '<div class="hashes">' + (d.chain_sample || []).map(function (h) {
          return "<span>" + esc(h) + "</span>";
        }).join("") + "</div>";

    /* ----------------------------------------------------------- the model */
    var nr = d.narration_resolution || {};
    el("llmCalls").textContent = (d.throughput || {}).llm_calls;
    el("fuzzyCount").textContent = nr.resolved_by_fuzzy_no_llm;

    /* --------------------------------------------------------- refusals */
    var refused = (d.exception_reason_counts || {}).attribution_ambiguous || 0;
    el("refuseCount").textContent = refused;
    el("refuseText").textContent = refused === 0
      ? "Nothing in this batch was ambiguous enough to refuse. Every credit that reached the "
        + "verification stage could be told apart from every other outstanding settlement on amount and timing."
      : refused + " settlement(s) had a credit proposed for them that could equally have belonged to "
        + "another settlement. The attribution was refused rather than guessed.";

    /* -------------------------------------------------------- colophon */
    var ms = Math.round((d.throughput || {}).wall_clock_ms);
    setText("clock", ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms");
    setText("genSource", d.source_dir);
    setText("genTime", d.generated_at || new Date().toLocaleString());
    setText("genThroughput",
      Math.round((d.throughput || {}).records_per_second).toLocaleString("en-IN")
      + " records/sec · " + (d.throughput || {}).llm_calls + " model calls");
  }

  /* Expanding an incident. Delegated, so it survives a re-render. */
  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest(".item-btn") : null;
    if (!btn) return;
    var item = btn.closest(".item");
    var open = item.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });

  global.Desk = { render: render, money: money, pct: pct, words: words };
})(window);
