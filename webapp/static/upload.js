/* The intake screen.
 *
 * Takes three files, posts them, hands the report to Desk.render. It does no
 * reconciliation of its own and never will: the moment a browser starts
 * deciding whether money matches, there are two implementations of the thing
 * this project exists to get right.
 */
(function () {
  "use strict";

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


  var SLOTS = ["ledger", "settlements", "bank"];
  var picked = {};
  var lastRun = null;
  var progressTimer = null;

  var el = function (id) { return document.getElementById(id); };
  var form = el("form");

  function size(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function readiness() {
    var missing = SLOTS.filter(function (s) { return !picked[s]; });
    el("run").disabled = missing.length > 0;
    el("readiness").textContent = missing.length
      ? "Still need: " + missing.join(", ")
      : "Ready to reconcile.";
  }

  function showFile(slot, file) {
    var drop = document.querySelector('.drop[data-slot="' + slot + '"]');
    drop.classList.remove("bad");
    drop.classList.add("filled");
    var line = drop.querySelector(".file");
    if (!line) {
      line = document.createElement("span");
      line.className = "file";
      drop.appendChild(line);
    }
    var cols = drop.querySelector(".cols");
    if (cols) cols.style.display = "none";
    line.innerHTML = "";
    line.appendChild(document.createTextNode(file.name));
    var sz = document.createElement("span");
    sz.className = "sz";
    sz.textContent = size(file.size);
    line.appendChild(sz);
  }

  /* wiring: click-to-pick and drag-and-drop, per slot */
  SLOTS.forEach(function (slot) {
    var drop = document.querySelector('.drop[data-slot="' + slot + '"]');
    var input = drop.querySelector("input[type=file]");

    input.addEventListener("change", function () {
      if (!input.files.length) return;
      picked[slot] = input.files[0];
      showFile(slot, input.files[0]);
      readiness();
    });

    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        drop.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        drop.classList.remove("over");
      });
    });
    drop.addEventListener("drop", function (e) {
      var file = e.dataTransfer && e.dataTransfer.files[0];
      if (!file) return;
      picked[slot] = file;
      // keep the real input in sync so a plain form submit would still work
      try {
        var dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
      } catch (err) { /* older browsers: the fetch path below still has it */ }
      showFile(slot, file);
      readiness();
    });
  });

  /* ------------------------------------------------------------ states */

  var STAGES = [
    "Parsing CSV sources & verifying column headers…",
    "Cross-matching accounting ledger vs Razorpay settlements…",
    "Verifying bank credits & parsing settlement UTRs…",
    "Evaluating period close conditions & generating audit trail…"
  ];

  /* The waiting scene. It animates the shape of the pipeline — three sources,
   * one gate, an appended chain, a named exception — and nothing else. Every
   * record chip is masked (ORD ····), so no number reaches the screen that a
   * real run did not produce. */
  var LANES = [
    { x: 64,  cls: "rec-ledger", text: "ORD ····   ₹ ·····" },
    { x: 240, cls: "rec-rzp",    text: "SETL ····  UTR ······" },
    { x: 416, cls: "rec-bank",   text: "NEFT/UTR ······ CR" }
  ];
  var GATE_X = 240, GATE_Y = 186;
  var CHAIN_MAX = 7, GUTTER_MAX = 4;
  var scene = { on: false, timer: null, n: 0 };

  function stillMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function spawn(lane) {
    if (!scene.on) return;
    var chip = document.createElement("span");
    chip.className = "rec " + lane.cls;
    chip.style.left = lane.x + "px";
    chip.style.setProperty("--dx", (GATE_X - lane.x) + "px");
    chip.style.setProperty("--dy", (GATE_Y - 94) + "px");
    chip.innerHTML = '<i></i>' + lane.text;
    chip.addEventListener("animationend", function () { chip.remove(); });
    el("stream").appendChild(chip);
  }

  function settle() {
    if (!scene.on) return;
    var gate = el("gate");
    gate.classList.remove("hit");
    void gate.offsetWidth;
    gate.classList.add("hit");

    /* one in five trios does not clear the gate — an exception is named, not dropped */
    if (scene.n % 5 === 4) {
      var stack = el("gutter");
      var mark = document.createElement("span");
      mark.className = "exc";
      mark.textContent = "✕";
      stack.appendChild(mark);
      while (stack.children.length > GUTTER_MAX) stack.removeChild(stack.firstChild);
      return;
    }

    var chain = el("chain");
    var link = document.createElement("span");
    link.className = "link";
    link.innerHTML = '<span class="blk">✓</span>';
    chain.appendChild(link);
    var live = chain.querySelectorAll(".link:not(.leaving)");
    if (live.length > CHAIN_MAX) {
      var gone = live[0];
      gone.classList.add("leaving");
      gone.querySelector(".blk").classList.add("leaving");
      setTimeout(function () { gone.remove(); }, 380);
    }
  }

  function cycle() {
    if (!scene.on) return;
    LANES.forEach(function (lane, i) {
      setTimeout(function () { spawn(lane); }, i * 110);
    });
    setTimeout(function () { settle(); scene.n += 1; }, 1320);
  }

  function startScene() {
    var stream = el("stream");
    if (!stream) return;
    stopScene();
    stream.innerHTML = "";
    el("chain").innerHTML = "";
    el("gutter").innerHTML = "";
    scene.on = true;
    scene.n = 0;
    if (stillMotion()) {
      /* the same picture, held still */
      for (var i = 0; i < 4; i++) {
        var link = document.createElement("span");
        link.className = "link";
        link.innerHTML = '<span class="blk">✓</span>';
        el("chain").appendChild(link);
      }
      var mark = document.createElement("span");
      mark.className = "exc";
      mark.textContent = "✕";
      el("gutter").appendChild(mark);
      return;
    }
    cycle();
    scene.timer = setInterval(cycle, 1150);
  }

  function stopScene() {
    scene.on = false;
    if (scene.timer) { clearInterval(scene.timer); scene.timer = null; }
    var stream = el("stream");
    if (stream) stream.innerHTML = "";
  }

  function busy(on, text) {
    var working = el("working");
    working.classList.toggle("hidden", !on);
    if (progressTimer) {
      clearInterval(progressTimer);
      progressTimer = null;
    }

    /* the waiting scene is decoration: a fault in it must never stop a run */
    try {
      if (on) { startScene(); } else { stopScene(); }
    } catch (err) {
      if (window.console) console.error("reconcile scene:", err);
    }

    if (on) {
      var stepIdx = 0;
      el("workingText").textContent = text || STAGES[0];
      progressTimer = setInterval(function () {
        stepIdx = (stepIdx + 1) % STAGES.length;
        el("workingText").textContent = STAGES[stepIdx];
      }, 700);
    }

    el("run").disabled = on || SLOTS.some(function (s) { return !picked[s]; });
    el("sample").disabled = on;
  }

  function clearProblem() {
    el("problem").classList.add("hidden");
    SLOTS.forEach(function (s) {
      document.querySelector('.drop[data-slot="' + s + '"]').classList.remove("bad");
    });
  }

  function showProblem(payload) {
    var box = el("problem");
    var html = "<h3>" + esc(payload.title || "That didn't work") + "</h3>";
    html += "<p>" + esc(payload.detail || "") + "</p>";
    if (payload.missing_columns && payload.missing_columns.length) {
      html += "<p>Missing column(s): " + payload.missing_columns.map(function (c) {
        return "<code>" + esc(c) + "</code>";
      }).join(" ") + "</p>";
    }
    if (payload.fix) html += '<p class="fix">' + esc(payload.fix) + "</p>";
    box.innerHTML = html;
    box.classList.remove("hidden");
    if (payload.slot) {
      var drop = document.querySelector('.drop[data-slot="' + payload.slot + '"]');
      if (drop) {
        drop.classList.add("bad");
        drop.classList.remove("filled");
      }
    }
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function showResults(payload) {
    lastRun = payload;
    el("intake").classList.add("hidden");
    el("results").classList.remove("hidden");
    el("ranFiles").textContent = payload.report.source_dir;
    Desk.render(payload.report);
    window.scrollTo(0, 0);
  }

  /* -------------------------------------------------------------- runs */

  function post(url, body) {
    clearProblem();
    busy(true, "Reconciling sources…");
    return fetch(url, body ? { method: "POST", body: body } : { method: "POST" })
      .then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      })
      .then(function (res) {
        busy(false);
        if (!res.ok) { showProblem(res.data); return; }
        try {
          showResults(res.data);
        } catch (err) {
          showProblem({
            title: "The reconciliation ran, but the page could not draw it",
            detail: String(err),
            fix: "The result itself is fine — this is a display fault. The browser console has the stack."
          });
          throw err;
        }
      })
      .catch(function (err) {
        busy(false);
        if (el("problem").classList.contains("hidden")) {
          showProblem({
            title: "Could not reach the reconciler",
            detail: String(err),
            fix: "Is the server still running in your terminal?"
          });
        }
      });
  }

  /* ---------------------------------------------------- the mapping step */

  var chosen = {};     // {slot: {field: their column}}

  function baseBody() {
    var body = new FormData();
    SLOTS.forEach(function (s) { body.append(s, picked[s]); });
    var holidays = el("holidays").files[0];
    if (holidays) body.append("holidays", holidays);
    body.append("use_llm", el("useLlm").checked ? "1" : "0");
    body.append("llm_schema", el("llmSchema").checked ? "1" : "0");
    return body;
  }

  function runReconcile() {
    var body = baseBody();
    SLOTS.forEach(function (s) {
      if (chosen[s] && Object.keys(chosen[s]).length) {
        body.append("mapping_" + s, JSON.stringify(chosen[s]));
      }
    });
    post("/api/reconcile", body);
  }

  function mapReadiness() {
    var missing = 0;
    SLOTS.forEach(function (slot) {
      var need = (window.__inspect[slot].unresolved || [])
        .filter(function (u) { return u.required; });
      need.forEach(function (u) {
        if (!(chosen[slot] || {})[u.field]) missing++;
      });
    });
    el("mapRun").disabled = missing > 0;
    el("mapReadiness").textContent = missing
      ? missing + " still to choose."
      : "Ready.";
    return missing;
  }

  function renderMapping(sources) {
    window.__inspect = sources;
    chosen = {};
    var host = el("mapFiles");
    host.innerHTML = "";

    SLOTS.forEach(function (slot) {
      var info = sources[slot];
      chosen[slot] = {};
      Object.keys(info.mapping).forEach(function (f) {
        chosen[slot][f] = info.mapping[f];
      });

      var need = (info.unresolved || []).filter(function (u) { return u.required; });
      var optional = (info.unresolved || []).filter(function (u) { return !u.required; });

      var box = document.createElement("div");
      box.className = "map-file";

      var head = document.createElement("header");
      head.innerHTML = "<div><h3>" + esc(info.label).replace(/^./, function (c) {
        return c.toUpperCase();
      }) + '</h3><div class="fname">' + esc(info.filename) + "</div></div>"
        + '<span class="state ' + (need.length ? "todo" : "ok") + '">'
        + (need.length ? need.length + " to choose" : "all recognised") + "</span>";
      box.appendChild(head);

      if (info.split_amount) {
        var note = document.createElement("div");
        note.className = "map-note";
        note.innerHTML = "This statement keeps money in two columns — <code>"
          + esc(info.split_amount.debit) + "</code> and <code>"
          + esc(info.split_amount.credit)
          + "</code>. They will be combined into one amount, and the direction kept, "
          + "so a reversal is still recognised as one.";
        box.appendChild(note);
      }

      var rows = document.createElement("div");
      rows.className = "map-rows";

      need.concat(optional).forEach(function (u) {
        var row = document.createElement("div");
        row.className = "map-row";

        var label = document.createElement("div");
        label.className = "field";
        label.innerHTML = esc(u.field) + (u.required ? '<span class="req">*</span>' : "");
        row.appendChild(label);

        var select = document.createElement("select");
        select.innerHTML = '<option value="">— not in this file —</option>'
          + info.columns.map(function (c) {
              return '<option value="' + esc(c) + '">' + esc(c) + "</option>";
            }).join("");
        if (u.suggestion) select.value = u.suggestion;
        if (u.required && !select.value) select.classList.add("unset");

        select.addEventListener("change", function () {
          if (select.value) {
            chosen[slot][u.field] = select.value;
            select.classList.remove("unset");
          } else {
            delete chosen[slot][u.field];
            if (u.required) select.classList.add("unset");
          }
          mapReadiness();
        });
        // a suggestion is pre-selected but still counts as chosen only once it
        // is in the box in front of the person, which it now is
        if (select.value) chosen[slot][u.field] = select.value;
        row.appendChild(select);

        var hint = document.createElement("div");
        hint.className = "hint";
        if (u.suggestion) {
          hint.innerHTML = (u.from_model ? "Model's reading: <b>" : "Best guess: <b>")
            + esc(u.suggestion) + "</b>. Change it if that is wrong.";
        } else if (u.note) {
          hint.textContent = u.note;
        } else {
          hint.textContent = "No column looked like this one.";
        }
        row.appendChild(hint);

        rows.appendChild(row);
      });

      if (!rows.children.length) {
        var ok = document.createElement("div");
        ok.className = "map-rows";
        ok.innerHTML = '<div class="map-row resolved"><div class="field">'
          + Object.keys(info.mapping).length + " columns matched</div>"
          + '<div class="hint">Nothing to choose for this file.</div></div>';
        box.appendChild(ok);
      } else {
        box.appendChild(rows);
      }

      host.appendChild(box);
    });

    el("intake").classList.add("hidden");
    el("mapping").classList.remove("hidden");
    mapReadiness();
    window.scrollTo(0, 0);
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    clearProblem();
    busy(true, "Reading the column headers…");

    fetch("/api/inspect", { method: "POST", body: baseBody() })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, data: d }; });
      })
      .then(function (res) {
        busy(false);
        if (!res.ok) { showProblem(res.data); return; }

        var sources = res.data.sources;
        var everythingPlaced = SLOTS.every(function (s) {
          return sources[s].ready && !(sources[s].unresolved || [])
            .some(function (u) { return u.required; });
        });
        // nothing to ask: go straight through, same as before
        if (everythingPlaced) { runReconcile(); return; }
        renderMapping(sources);
      })
      .catch(function (err) {
        busy(false);
        showProblem({
          title: "Could not reach the reconciler",
          detail: String(err),
          fix: "Is the server still running in your terminal?"
        });
      });
  });

  el("mapRun").addEventListener("click", function () {
    if (mapReadiness() > 0) return;
    el("mapping").classList.add("hidden");
    el("intake").classList.remove("hidden");
    runReconcile();
  });

  el("mapBack").addEventListener("click", function () {
    el("mapping").classList.add("hidden");
    el("intake").classList.remove("hidden");
    window.scrollTo(0, 0);
  });

  el("sample").addEventListener("click", function () {
    post("/api/sample/realistic");
  });

  el("again").addEventListener("click", function () {
    el("results").classList.add("hidden");
    el("mapping").classList.add("hidden");
    el("intake").classList.remove("hidden");
    window.scrollTo(0, 0);
  });

  function saveBlob(name, blob) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 0);
  }

  // The PDF is typeset on the server, from the run this page is already
  // holding -- posted back rather than re-reconciled, so the document and the
  // screen can never disagree about a number.
  function downloadPdf(button, endpoint, name) {
    if (!lastRun) return;
    var label = button.textContent;
    button.disabled = true;
    button.textContent = "Preparing PDF…";
    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lastRun)
    })
      .then(function (r) {
        if (!r.ok) {
          return r.json().then(function (p) {
            throw new Error(p.detail || p.title || "the server refused it");
          });
        }
        return r.blob();
      })
      .then(function (blob) { saveBlob(name, blob); })
      .catch(function (err) {
        showProblem({
          title: "That PDF could not be produced",
          detail: String(err.message || err),
          fix: "The reconciliation itself is unaffected — the result on screen still stands."
        });
      })
      .then(function () {
        button.disabled = false;
        button.textContent = label;
      });
  }

  el("dlReport").addEventListener("click", function () {
    downloadPdf(this, "/api/report.pdf", "reconciliation_close_pack.pdf");
  });
  el("dlAudit").addEventListener("click", function () {
    downloadPdf(this, "/api/audit.pdf", "audit_trail.pdf");
  });

  readiness();
})();
