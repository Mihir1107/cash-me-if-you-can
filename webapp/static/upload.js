/* The intake screen.
 *
 * Takes three files, posts them, hands the report to Desk.render. It does no
 * reconciliation of its own and never will: the moment a browser starts
 * deciding whether money matches, there are two implementations of the thing
 * this project exists to get right.
 */
(function () {
  "use strict";

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

  function busy(on, text) {
    var working = el("working");
    working.classList.toggle("hidden", !on);
    if (progressTimer) {
      clearInterval(progressTimer);
      progressTimer = null;
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
    var html = "<h3>" + (payload.title || "That didn't work") + "</h3>";
    html += "<p>" + (payload.detail || "") + "</p>";
    if (payload.missing_columns && payload.missing_columns.length) {
      html += "<p>Missing column(s): " + payload.missing_columns.map(function (c) {
        return "<code>" + c + "</code>";
      }).join(" ") + "</p>";
    }
    if (payload.fix) html += '<p class="fix">' + payload.fix + "</p>";
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

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var body = new FormData();
    SLOTS.forEach(function (s) { body.append(s, picked[s]); });
    var holidays = el("holidays").files[0];
    if (holidays) body.append("holidays", holidays);
    body.append("use_llm", el("useLlm").checked ? "1" : "0");
    post("/api/reconcile", body);
  });

  el("sample").addEventListener("click", function () {
    post("/api/sample/realistic");
  });

  el("again").addEventListener("click", function () {
    el("results").classList.add("hidden");
    el("intake").classList.remove("hidden");
    window.scrollTo(0, 0);
  });

  function download(name, text) {
    var blob = new Blob([text], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 0);
  }

  el("dlReport").addEventListener("click", function () {
    if (lastRun) download("reconciliation_report.json", JSON.stringify(lastRun.report, null, 2));
  });
  el("dlAudit").addEventListener("click", function () {
    if (lastRun) download("audit_trail.jsonl", lastRun.audit_trail || "");
  });

  readiness();
})();
