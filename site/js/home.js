/* Home page counters, sector bars, and a short leaderboard preview. */
(function (root) {
  var MR = root.MR;
  var ARC = 2 * Math.PI * 50;

  function paintArc(id, startFrac, frac) {
    var node = document.getElementById(id);
    if (!node) return;
    var span = Math.max(0, frac) * ARC;
    node.setAttribute("stroke-dasharray", span + " " + Math.max(0, ARC - span));
    node.setAttribute("stroke-dashoffset", String(-Math.max(0, startFrac) * ARC));
  }

  function tween(node, to) {
    if (!node) return;
    var from = Number(node.dataset.n || 0);
    node.dataset.n = String(to);
    if (MR.reduceMotion() || from === to) {
      node.textContent = MR.fmt(to);
      return;
    }
    var start = root.performance.now();
    function step(now) {
      var p = Math.min(1, (now - start) / 480);
      var eased = 1 - Math.pow(1 - p, 3);
      node.textContent = MR.fmt(from + (to - from) * eased);
      if (p < 1) root.requestAnimationFrame(step);
    }
    root.requestAnimationFrame(step);
  }

  function bar(parent, cls, value, total) {
    if (!total || value <= 0) return;
    var seg = document.createElement("i");
    seg.className = cls;
    seg.style.width = (100 * value / total) + "%";
    parent.appendChild(seg);
  }

  function renderSectors(stats) {
    var host = document.getElementById("sectors");
    if (!host) return;
    host.replaceChildren();
    if (!stats.sectors.length) {
      host.appendChild(MR.el("p", "empty", "No sectors in this feed yet."));
      return;
    }
    stats.sectors.forEach(function (sector) {
      var row = MR.el("div", "sector");
      var total = sector.total || (sector.agreed + sector.awaiting + sector.progressing);
      row.setAttribute(
        "aria-label",
        "Sector " + sector.sector + ": " + MR.fmt(sector.agreed) + " of " + MR.fmt(total) + " stars agreed"
      );
      row.appendChild(MR.el("b", "", "Sector " + sector.sector));
      var stack = MR.el("div", "stack");
      stack.setAttribute("aria-hidden", "true");
      bar(stack, "verified", sector.agreed, total);
      bar(stack, "submitted", sector.awaiting, total);
      bar(stack, "wip", sector.progressing, total);
      row.appendChild(stack);
      var note = MR.el(
        "p",
        "sector-note",
        MR.fmt(sector.agreed) + " agreed · " + MR.fmt(sector.awaiting) + " awaiting a second check · " + MR.fmt(sector.progressing) + " in progress"
      );
      row.appendChild(note);
      host.appendChild(row);
    });
  }

  function renderBoard(stats) {
    var list = document.getElementById("home-board");
    var block = document.getElementById("home-board-block");
    if (!list || !block) return;
    list.replaceChildren();
    var rows = stats.leaderboard.slice(0, 5);
    if (!rows.length) {
      block.hidden = true;
      return;
    }
    block.hidden = false;
    rows.forEach(function (row, index) {
      var item = MR.el("li", "board-row");
      item.appendChild(MR.el("span", "rank", String(index + 1)));
      var who = MR.el("span", "name");
      who.appendChild(MR.el("span", "", row.name));
      item.appendChild(who);
      var stars = MR.el("span", "metric");
      stars.appendChild(MR.el("b", "", MR.fmt(row.starsAgreed)));
      stars.appendChild(MR.el("span", "", "Stars agreed"));
      item.appendChild(stars);
      list.appendChild(item);
    });
  }

  function render(stats, meta) {
    if (meta.sample) MR.setFeedStatus("sample");
    else if (meta.stale) MR.setFeedStatus("stale");
    else MR.setFeedStatus("live");

    tween(document.getElementById("n-agreed"), stats.starsAgreed);
    tween(document.getElementById("n-await"), stats.starsAwaiting);
    tween(document.getElementById("n-cands"), stats.candidatesUnverified);
    tween(document.getElementById("n-online"), stats.online);

    var total = stats.starsTotal;
    var agreedF = total ? stats.starsAgreed / total : 0;
    var awaitF = total ? stats.starsAwaiting / total : 0;
    var progF = total ? stats.starsInProgress / total : 0;
    paintArc("arc-ok", 0, agreedF);
    paintArc("arc-sub", agreedF, awaitF);
    paintArc("arc-wip", agreedF + awaitF, progF);
    var pct = document.getElementById("ring-pct");
    if (pct) pct.textContent = Math.round(agreedF * 100) + "%";
    var line = document.getElementById("progline");
    if (line) {
      line.textContent = MR.fmt(stats.starsAgreed) + " of " + MR.fmt(total)
        + " stars have two agreeing checks. "
        + MR.fmt(stats.starsAwaiting) + " are waiting for a second person. "
        + MR.fmt(stats.starsInProgress) + " are in progress. "
        + MR.fmt(stats.batchesAgreed) + " of " + MR.fmt(stats.batchesTotal) + " batches agreed.";
    }
    var when = document.getElementById("feed-when");
    if (when) {
      var text = "";
      if (stats.updatedAt) {
        var parsed = Date.parse(stats.updatedAt);
        if (!isNaN(parsed)) {
          text = "Updated " + new Intl.DateTimeFormat("en", {
            dateStyle: "medium",
            timeStyle: "short"
          }).format(new Date(parsed));
        }
      }
      when.textContent = text;
    }
    renderSectors(stats);
    renderBoard(stats);
  }

  function boot() {
    if (!document.getElementById("stat-grid")) return;
    MR.watchStats(render);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})(typeof window !== "undefined" ? window : globalThis);
