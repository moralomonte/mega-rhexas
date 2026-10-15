/* Leaderboard from the public stats feed, ranked by agreed stars then display name. */
(function () {
  var MR = window.MR;

  function metric(value, label) {
    var node = MR.el("span", "metric");
    node.appendChild(MR.el("b", "", MR.fmt(value)));
    node.appendChild(MR.el("span", "", label));
    return node;
  }

  function render(stats, meta) {
    if (meta.sample) MR.setFeedStatus("sample");
    else if (meta.stale) MR.setFeedStatus("stale");
    else MR.setFeedStatus("live");

    var list = document.getElementById("board");
    if (!list) return;
    list.replaceChildren();
    if (!stats.leaderboard.length) {
      var empty = MR.el("li", "empty", "No public names yet. When volunteers have agreed work, display names show up here.");
      list.appendChild(empty);
      return;
    }
    stats.leaderboard.forEach(function (row, index) {
      var item = MR.el("li", "board-row");
      item.appendChild(MR.el("span", "rank", String(index + 1)));
      item.appendChild(MR.el("span", "name", row.name));
      var once = stats.copiesRequired === 1;
      item.appendChild(metric(row.starsAgreed, once ? "Stars checked once" : "Stars agreed"));
      item.appendChild(metric(row.batchesAgreed, once ? "Batches checked once" : "Batches agreed"));
      item.appendChild(metric(row.candidatesUnverified, "Unverified candidates"));
      list.appendChild(item);
    });
  }

  function boot() {
    if (!document.getElementById("board")) return;
    MR.watchStats(render);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
