/* Shared chrome: starfield, current nav item, session link, download link. */
(function (root) {
  var MR = root.MR || (root.MR = {});
  MR.API = "https://api.megarhexas.com";
  MR.RELEASES = "https://github.com/moralomonte/mega-rhexas/releases/latest";
  MR.GH_RELEASE_API = "https://api.github.com/repos/moralomonte/mega-rhexas/releases/latest";

  MR.fmt = function (n) {
    return Math.round(Number(n) || 0).toLocaleString("en-US");
  };

  MR.el = function (tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  MR.reduceMotion = function () {
    return root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches;
  };

  function pathName() {
    var path = root.location.pathname || "/";
    path = path.replace(/\/index\.html$/, "/").replace(/\.html$/, "");
    if (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
    return path || "/";
  }

  function markNav() {
    var here = pathName();
    document.querySelectorAll(".site-nav a").forEach(function (link) {
      var href = link.getAttribute("href") || "";
      if (href.charAt(0) !== "/") return;
      var target = href.replace(/\.html$/, "");
      if (target.length > 1 && target.endsWith("/")) target = target.slice(0, -1);
      if (target === here) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function stars() {
    var canvas = document.getElementById("stars");
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext("2d");
    var dots = [];
    var reduce = MR.reduceMotion();
    var queued = false;

    function resize() {
      canvas.width = root.innerWidth;
      canvas.height = root.innerHeight;
      var count = root.innerWidth < 720 ? 70 : 120;
      dots = [];
      for (var i = 0; i < count; i++) {
        dots.push({
          x: Math.random() * canvas.width,
          y: Math.random() * canvas.height,
          r: Math.random() * 1.15 + 0.2,
          a: Math.random() * Math.PI * 2,
          s: 0.004 + Math.random() * 0.012
        });
      }
    }

    function frame() {
      queued = false;
      var g = ctx.createRadialGradient(canvas.width * 0.8, 0, 0, canvas.width * 0.8, 0, canvas.width);
      g.addColorStop(0, "rgba(26,16,64,0.55)");
      g.addColorStop(0.55, "rgba(7,8,24,0.15)");
      g.addColorStop(1, "rgba(4,5,12,0)");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      for (var i = 0; i < dots.length; i++) {
        var star = dots[i];
        if (!reduce) star.a += star.s;
        ctx.globalAlpha = 0.25 + 0.7 * Math.abs(Math.sin(star.a));
        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        ctx.arc(star.x, star.y, star.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (!reduce && !document.hidden) schedule();
    }

    function schedule() {
      if (queued) return;
      queued = true;
      root.requestAnimationFrame(frame);
    }

    resize();
    root.addEventListener("resize", function () {
      resize();
      frame();
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !reduce) schedule();
    });
    frame();
  }

  function resolveDownload() {
    var links = document.querySelectorAll(".js-download");
    if (!links.length) return;
    fetch(MR.GH_RELEASE_API, {
      headers: { Accept: "application/vnd.github+json" },
      credentials: "omit"
    }).then(function (res) {
      if (!res.ok) throw new Error("release");
      return res.json();
    }).then(function (data) {
      var assets = data && Array.isArray(data.assets) ? data.assets : [];
      var installer = null;
      for (var i = 0; i < assets.length; i++) {
        var name = String(assets[i].name || "");
        if (/\.(exe|msi|dmg|pkg|deb|rpm|AppImage)$/i.test(name) || /install/i.test(name)) {
          installer = assets[i];
          break;
        }
      }
      var href = installer && installer.browser_download_url
        ? installer.browser_download_url
        : (data && data.html_url) || MR.RELEASES;
      links.forEach(function (link) {
        link.href = href;
      });
      var note = document.getElementById("download-note");
      if (note && installer && installer.name) {
        note.textContent = "Latest GitHub Release installer: " + installer.name;
      }
    }).catch(function () {
      /* The href already points at /releases/latest. */
    });
  }

  /**
   * Session probe. A 401 means logged out. Network and other errors leave the
   * Log in link in place; account pages explain that the service did not answer.
   */
  MR.session = function () {
    return fetch(MR.API + "/api/v1/me", {
      credentials: "include",
      headers: { Accept: "application/json" },
      cache: "no-store"
    }).then(function (res) {
      if (res.status === 401 || res.status === 403) return { state: "out" };
      if (!res.ok) return { state: "down", status: res.status };
      return res.json().then(function (payload) {
        var src = payload.account || payload.user || payload;
        return {
          state: "in",
          account: {
            id: src.id || src.account_id || "",
            email: src.email || "",
            displayName: src.display_name || src.displayName || src.name || ""
          }
        };
      });
    }).catch(function () {
      return { state: "down" };
    });
  };

  function paintSession() {
    var link = document.getElementById("nav-session");
    if (!link) return;
    MR.session().then(function (session) {
      if (session.state !== "in") return;
      link.textContent = "My account";
      link.href = "/account";
      link.id = "nav-session";
      markNav();
    });
  }

  function boot() {
    markNav();
    stars();
    resolveDownload();
    paintSession();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})(typeof window !== "undefined" ? window : globalThis);
