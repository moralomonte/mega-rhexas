/* Public stats client.
 *
 * GET https://api.megarhexas.com/api/v1/public/stats
 *
 * TODO(public-stats): another change is adding this route on the coordinator.
 * Preferred JSON (unknown extra fields are ignored; the aliases below are read):
 * {
 *   "updated_at": "2026-01-01T00:00:00Z",
 *   "stars_verified": 0,         // tics_agreed, stars_agreed
 *   "stars_awaiting": 0,         // tics_submitted, stars_submitted
 *   "stars_in_progress": 0,      // tics_in_progress
 *   "stars_total": 0,            // tics_total
 *   "candidates_unverified": 0,  // candidates_new, unverified_candidates
 *   "volunteers_online": 0,      // workers_online, accounts_online
 *   "batches_agreed": 0,         // units_agreed
 *   "batches_total": 0,          // units_total
 *   "sectors": [{
 *     "sector": 107,
 *     "tics": 0, "tics_agreed": 0, "tics_submitted": 0, "tics_in_progress": 0
 *   }],
 *   "leaderboard": [{
 *     "display_name": "Ada",     // displayName, name
 *     "stars_verified": 0,       // stars_agreed, tics_agreed
 *     "batches_verified": 0,     // units_agreed, batches_agreed
 *     "candidates_unverified": 0 // candidates_new
 *   }]
 * }
 * Rank is stars_verified descending, then display name (en). The visible name
 * is the account display name, never an email.
 * A failed response uses labeled sample numbers. A 200 with empty lists is live.
 */
(function (root) {
  var MR = root.MR || (root.MR = {});

  function num() {
    for (var i = 0; i < arguments.length; i++) {
      var value = arguments[i];
      if (typeof value === "number" && isFinite(value)) return value;
      if (typeof value === "string" && value.trim() && isFinite(Number(value))) return Number(value);
    }
    return 0;
  }

  function normalizeSector(row) {
    row = row || {};
    return {
      sector: num(row.sector, row.sector_id, row.id),
      total: num(row.tics, row.stars, row.stars_total, row.total),
      agreed: num(row.tics_agreed, row.stars_verified, row.stars_agreed, row.agreed),
      awaiting: num(row.tics_submitted, row.stars_awaiting, row.stars_submitted, row.awaiting, row.submitted),
      progressing: num(row.tics_in_progress, row.stars_in_progress, row.in_progress)
    };
  }

  function normalizeRow(row) {
    row = row || {};
    var name = row.display_name || row.displayName || row.name || "";
    name = String(name).trim();
    return {
      name: name,
      starsAgreed: num(row.stars_verified, row.stars_agreed, row.tics_agreed),
      batchesAgreed: num(row.batches_verified, row.batches_agreed, row.units_agreed),
      candidatesUnverified: num(row.candidates_unverified, row.candidates_new, row.unverified_candidates)
    };
  }

  MR.normalizeStats = function (raw) {
    var rootObj = raw && typeof raw === "object" ? raw : {};
    var src = rootObj.stats && typeof rootObj.stats === "object"
      ? Object.assign({}, rootObj, rootObj.stats)
      : rootObj;
    var sectorsIn = src.sectors || src.sector_progress || [];
    var boardIn = src.leaderboard || src.accounts || src.volunteers || [];
    var sectors = (Array.isArray(sectorsIn) ? sectorsIn : []).map(normalizeSector).filter(function (row) {
      return row.sector > 0;
    });
    sectors.sort(function (a, b) { return b.sector - a.sector; });
    var board = (Array.isArray(boardIn) ? boardIn : []).map(normalizeRow).filter(function (row) {
      return row.name;
    });
    board.sort(function (a, b) {
      return b.starsAgreed - a.starsAgreed || a.name.localeCompare(b.name, "en");
    });
    return {
      starsAgreed: num(src.stars_verified, src.tics_agreed, src.stars_agreed),
      starsAwaiting: num(src.stars_awaiting, src.tics_submitted, src.stars_submitted),
      starsInProgress: num(src.stars_in_progress, src.tics_in_progress),
      starsTotal: num(src.stars_total, src.tics_total),
      candidatesUnverified: num(src.candidates_unverified, src.candidates_new, src.unverified_candidates),
      online: num(src.volunteers_online, src.workers_online, src.accounts_online),
      batchesAgreed: num(src.batches_agreed, src.units_agreed),
      batchesTotal: num(src.batches_total, src.units_total),
      sectors: sectors,
      leaderboard: board,
      updatedAt: src.updated_at || src.updatedAt || null,
      copiesRequired: (function () {
        var copies = num(src.copies_required);
        if (copies === 1 || copies === 2) return copies;
        return num(src.replicas) === 1 ? 1 : 2;
      })()
    };
  };

  /* Placeholders only. Shown when the public feed does not answer, and labeled as such. */
  MR.SAMPLE_STATS = MR.normalizeStats({
    stars_verified: 18420,
    stars_awaiting: 5360,
    stars_in_progress: 2104,
    stars_total: 96240,
    candidates_unverified: 11,
    volunteers_online: 4,
    batches_agreed: 36,
    batches_total: 192,
    sectors: [
      { sector: 107, tics: 20000, tics_agreed: 4200, tics_submitted: 1800, tics_in_progress: 900 },
      { sector: 106, tics: 19840, tics_agreed: 6100, tics_submitted: 1540, tics_in_progress: 640 },
      { sector: 105, tics: 19120, tics_agreed: 4800, tics_submitted: 1220, tics_in_progress: 404 },
      { sector: 104, tics: 18640, tics_agreed: 2100, tics_submitted: 500, tics_in_progress: 120 },
      { sector: 103, tics: 18640, tics_agreed: 1220, tics_submitted: 300, tics_in_progress: 40 }
    ],
    leaderboard: [
      { display_name: "Mira", stars_verified: 4120, batches_verified: 9, candidates_unverified: 2 },
      { display_name: "Sol", stars_verified: 3004, batches_verified: 7, candidates_unverified: 1 },
      { display_name: "Ivo", stars_verified: 2440, batches_verified: 5, candidates_unverified: 3 },
      { display_name: "Nia", stars_verified: 1102, batches_verified: 3, candidates_unverified: 0 },
      { display_name: "Ren", stars_verified: 860, batches_verified: 2, candidates_unverified: 1 },
      { display_name: "Ada", stars_verified: 860, batches_verified: 2, candidates_unverified: 0 },
      { display_name: "Bo", stars_verified: 540, batches_verified: 1, candidates_unverified: 0 }
    ]
  });

  MR.loadStats = function () {
    return fetch(MR.API + "/api/v1/public/stats", {
      credentials: "omit",
      headers: { Accept: "application/json" },
      cache: "no-store"
    }).then(function (res) {
      if (!res.ok) throw new Error("stats " + res.status);
      return res.json();
    }).then(function (payload) {
      return { live: true, stats: MR.normalizeStats(payload) };
    });
  };

  var lastLive = null;

  MR.watchStats = function (onData) {
    var timer = 0;

    function apply(result) {
      if (result.live) {
        lastLive = result.stats;
        onData(result.stats, { sample: false, stale: false });
        return;
      }
      if (lastLive) onData(lastLive, { sample: false, stale: true });
      else onData(MR.SAMPLE_STATS, { sample: true, stale: false });
    }

    function tick() {
      MR.loadStats().then(function (result) {
        apply(result);
      }).catch(function () {
        apply({ live: false });
      });
    }

    tick();
    timer = root.setInterval(tick, 30000);
    root.addEventListener("pagehide", function () {
      root.clearInterval(timer);
    });
  };

  MR.setFeedStatus = function (mode) {
    var banner = document.getElementById("sample-banner");
    var label = document.getElementById("feed-label");
    var dot = document.getElementById("feed-dot");
    if (banner) banner.hidden = mode !== "sample";
    if (!label || !dot) return;
    dot.className = "dot";
    if (mode === "live") {
      label.textContent = "Live";
      dot.classList.add("live");
    } else if (mode === "sample") {
      label.textContent = "Sample preview";
      dot.classList.add("sample");
    } else if (mode === "stale") {
      label.textContent = "Reconnecting";
      dot.classList.add("wait");
    } else {
      label.textContent = "Connecting…";
    }
  };
})(typeof window !== "undefined" ? window : globalThis);
