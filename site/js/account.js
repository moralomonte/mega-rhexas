/* Account pages: sign up, log in, my account, link a device.
 *
 * Routes are the coordinator's JSON API (see DEPLOY.md, "Account routes").
 * Credentials are session cookies (fetch credentials: "include"). The API
 * allows https://megarhexas.com and https://www.megarhexas.com with
 * Access-Control-Allow-Credentials.
 *
 * POST /api/v1/signup   { email, password, display_name, invite_code } -> 201
 * POST /api/v1/login    { email, password } -> 200
 * POST /api/v1/logout   -> 204
 * GET  /api/v1/me       -> { id, email, display_name, ... } or 401
 * GET  /api/v1/me/devices -> { devices: [{ id, name, hostname, kind, created_at, last_seen_at, revoked_at }] }
 * POST /api/v1/me/devices/{id}/revoke -> { status: "revoked" }
 * GET  /api/v1/me/stats -> stars_verified, stars_pending, batches_verified, batches_pending, ...
 * GET  /api/v1/device?code=   -> { hostname, status, code }
 * POST /api/v1/device/approve { code } -> hostname and device id (no token)
 *
 * Devices are linked by the worker's device flow; the browser never sees a token.
 * Personal stats are never replaced with the public sample numbers.
 */
(function () {
  var MR = window.MR;
  var PATHS = {
    signup: "/api/v1/signup",
    login: "/api/v1/login",
    logout: "/api/v1/logout",
    me: "/api/v1/me",
    devices: "/api/v1/me/devices",
    stats: "/api/v1/me/stats",
    device: function (code) {
      return "/api/v1/device?code=" + encodeURIComponent(code);
    },
    approve: "/api/v1/device/approve",
    revoke: function (id) {
      return "/api/v1/me/devices/" + encodeURIComponent(id) + "/revoke";
    }
  };

  function page() {
    return document.body.getAttribute("data-page") || "";
  }

  function show(node, kind, message) {
    if (!node) return;
    node.className = "alert " + kind;
    node.textContent = message || "";
    node.hidden = !message;
    if (message) node.focus();
  }

  function safeMessage(payload, fallback) {
    var msg = payload && (payload.error || payload.message || payload.detail);
    if (typeof msg !== "string") return fallback;
    msg = msg.trim();
    if (!msg || msg.length > 240 || /[<>]/.test(msg)) return fallback;
    return msg;
  }

  function readJson(res) {
    return res.text().then(function (text) {
      if (!text) return {};
      try { return JSON.parse(text); }
      catch (err) { return {}; }
    });
  }

  function request(path, options) {
    var headers = { Accept: "application/json" };
    if (options && options.body) headers["Content-Type"] = "application/json";
    return fetch(MR.API + path, {
      method: (options && options.method) || "GET",
      credentials: "include",
      headers: headers,
      body: options && options.body ? JSON.stringify(options.body) : undefined,
      cache: "no-store"
    }).then(function (res) {
      return readJson(res).then(function (payload) {
        return { res: res, payload: payload };
      });
    }, function () {
      var error = new Error("Could not reach the account service at api.megarhexas.com. Try again in a moment.");
      error.network = true;
      throw error;
    });
  }

  function fallbackFor(status, action) {
    if (status === 404) return "That account route is not on the server yet.";
    if (status === 409) return "An account with that email already exists. Log in instead.";
    if (status === 429) return "Too many tries. Wait a moment and try again.";
    if (status >= 500) return "The account service had a problem. Try again later.";
    if (action === "login" && (status === 401 || status === 403)) return "That email or password was not accepted.";
    if (action === "signup" && (status === 401 || status === 403)) {
      return "The server did not accept the sign-up. Check the invite code. The beta is invite-only for now.";
    }
    return "The account service returned status " + status + ".";
  }

  function busy(button, on, label) {
    if (!button) return;
    if (!button.dataset.label) button.dataset.label = button.textContent;
    button.disabled = on;
    button.setAttribute("aria-busy", on ? "true" : "false");
    button.textContent = on ? label : button.dataset.label;
  }

  function accountOf(payload) {
    var src = (payload && (payload.account || payload.user)) || payload || {};
    return {
      id: src.id || src.account_id || "",
      email: src.email || "",
      displayName: src.display_name || src.displayName || src.name || ""
    };
  }

  function goAccountIfSession() {
    return MR.session().then(function (session) {
      if (session.state === "in") window.location.assign("/account");
    });
  }

  function bindSignup() {
    var form = document.getElementById("signup-form");
    if (!form) return;
    var alert = document.getElementById("form-alert");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      show(alert, "error", "");
      var button = form.querySelector("button[type=submit]");
      busy(button, true, "Creating account…");
      request(PATHS.signup, {
        method: "POST",
        body: {
          display_name: form.display_name.value.trim(),
          email: form.email.value.trim(),
          password: form.password.value,
          invite_code: form.invite_code.value.trim()
        }
      }).then(function (result) {
        if (!result.res.ok && result.res.status !== 204) {
          show(alert, "error", safeMessage(result.payload, fallbackFor(result.res.status, "signup")));
          return;
        }
        return MR.session().then(function (session) {
          if (session.state === "in") window.location.assign("/account");
          else window.location.assign("/login?created=1");
        });
      }).catch(function (err) {
        show(alert, "error", err.message);
      }).then(function () {
        busy(button, false);
      });
    });
  }

  function bindLogin() {
    var form = document.getElementById("login-form");
    if (!form) return;
    var alert = document.getElementById("form-alert");
    var params = new URLSearchParams(window.location.search);
    if (params.get("created") === "1") {
      show(alert, "ok", "Account created. Log in with the same email.");
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      show(alert, "error", "");
      var button = form.querySelector("button[type=submit]");
      busy(button, true, "Logging in…");
      request(PATHS.login, {
        method: "POST",
        body: {
          email: form.email.value.trim(),
          password: form.password.value
        }
      }).then(function (result) {
        if (!result.res.ok && result.res.status !== 204) {
          show(alert, "error", safeMessage(result.payload, fallbackFor(result.res.status, "login")));
          return;
        }
        window.location.assign("/account");
      }).catch(function (err) {
        show(alert, "error", err.message);
      }).then(function () {
        busy(button, false);
      });
    });
  }

  function formatWhen(iso) {
    if (!iso) return "";
    var parsed = Date.parse(iso);
    if (isNaN(parsed)) return "";
    return new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" }).format(new Date(parsed));
  }

  function numOf(obj) {
    var keys = Array.prototype.slice.call(arguments, 1);
    for (var i = 0; i < keys.length; i++) {
      var value = obj[keys[i]];
      if (typeof value === "number" && isFinite(value)) return value;
      if (typeof value === "string" && value.trim() && isFinite(Number(value))) return Number(value);
    }
    return null;
  }

  function paintStats(payload) {
    var host = document.getElementById("my-stats");
    if (!host) return;
    var src = (payload && payload.stats) || payload || {};
    var rows = [
      ["Stars agreed", numOf(src, "stars_verified", "stars_agreed", "tics_agreed")],
      ["Stars pending", numOf(src, "stars_pending", "stars_submitted", "tics_submitted")],
      ["Batches agreed", numOf(src, "batches_verified", "batches_agreed", "units_agreed")],
      ["Batches pending", numOf(src, "batches_pending", "batches_submitted")]
    ];
    host.replaceChildren();
    rows.forEach(function (row) {
      var cell = document.createElement("div");
      cell.appendChild(MR.el("span", "", row[0]));
      cell.appendChild(MR.el("b", "", row[1] == null ? "–" : MR.fmt(row[1])));
      host.appendChild(cell);
    });
  }

  function deviceItems(payload) {
    if (Array.isArray(payload)) return payload;
    if (payload && Array.isArray(payload.devices)) return payload.devices;
    return [];
  }

  function paintDevices(payload) {
    var list = document.getElementById("device-list");
    if (!list) return;
    list.replaceChildren();
    var devices = deviceItems(payload).filter(function (device) {
      return device && !device.revoked && !device.revoked_at;
    });
    if (!devices.length) {
      list.appendChild(MR.el("li", "empty", "No linked devices yet."));
      return;
    }
    devices.forEach(function (device) {
      var item = document.createElement("li");
      var text = document.createElement("div");
      var name = device.name || device.hostname || device.display_name || "Device";
      text.appendChild(MR.el("strong", "", name));
      var seen = formatWhen(device.last_seen_at || device.last_seen || device.linked_at || device.created_at);
      if (seen) text.appendChild(MR.el("div", "note", seen));
      item.appendChild(text);
      var button = MR.el("button", "btn btn-ghost btn-small", "Revoke");
      button.type = "button";
      button.setAttribute("aria-label", "Revoke " + name);
      button.addEventListener("click", function () {
        if (!window.confirm("Revoke this device? It will no longer be linked to your account.")) return;
        button.disabled = true;
        request(PATHS.revoke(device.id), { method: "POST" }).then(function (result) {
          if (!result.res.ok && result.res.status !== 204) {
            button.disabled = false;
            window.alert(safeMessage(result.payload, fallbackFor(result.res.status, "revoke")));
            return;
          }
          loadDevices();
        }).catch(function (err) {
          button.disabled = false;
          window.alert(err.message);
        });
      });
      item.appendChild(button);
      list.appendChild(item);
    });
  }

  function loadDevices() {
    return request(PATHS.devices).then(function (result) {
      var list = document.getElementById("device-list");
      if (!result.res.ok) {
        if (list) {
          list.replaceChildren();
          list.appendChild(MR.el("li", "empty", result.res.status === 404
            ? "The device list is not on the server yet."
            : "The device list did not load."));
        }
        return;
      }
      paintDevices(result.payload);
    }).catch(function () {
      var list = document.getElementById("device-list");
      if (!list) return;
      list.replaceChildren();
      list.appendChild(MR.el("li", "empty", "Could not reach the account service."));
    });
  }

  function loadMyStats() {
    var host = document.getElementById("my-stats");
    return request(PATHS.stats).then(function (result) {
      if (!result.res.ok) {
        if (host) {
          host.replaceChildren();
          host.appendChild(MR.el("div", "empty", result.res.status === 404
            ? "Your counts will show here when the stats route is available."
            : "Your counts did not load."));
        }
        return;
      }
      paintStats(result.payload);
    }).catch(function () {
      if (!host) return;
      host.replaceChildren();
      host.appendChild(MR.el("div", "empty", "Could not reach the account service."));
    });
  }

  function showLoggedOut(session) {
    var gate = document.getElementById("logged-out");
    var app = document.getElementById("logged-in");
    if (gate) gate.hidden = false;
    if (app) app.hidden = true;
    var note = document.getElementById("gate-note");
    if (!note) return;
    if (session && session.state === "down") {
      note.textContent = session.status === 404
        ? "The account service is not on the server yet. The beta is invite-only for now."
        : "Could not reach the account service at api.megarhexas.com. Try again in a moment.";
    } else {
      note.textContent = "Log in to see this page.";
    }
  }

  function bindAccount() {
    var gate = document.getElementById("logged-out");
    if (!gate) return;
    MR.session().then(function (session) {
      if (session.state !== "in") {
        showLoggedOut(session);
        return;
      }
      gate.hidden = true;
      var app = document.getElementById("logged-in");
      if (app) app.hidden = false;
      var name = document.getElementById("acct-name");
      var email = document.getElementById("acct-email");
      if (name) name.textContent = session.account.displayName || "Account";
      if (email) email.textContent = session.account.email || "";
      loadMyStats();
      loadDevices();
    });
    var logout = document.getElementById("logout");
    if (logout) {
      logout.addEventListener("click", function () {
        logout.disabled = true;
        request(PATHS.logout, { method: "POST" }).then(function () {
          window.location.assign("/");
        }).catch(function () {
          window.location.assign("/");
        });
      });
    }
  }

  function readToken(payload) {
    if (!payload) return "";
    if (typeof payload.token === "string") return payload.token;
    var device = payload.device || payload;
    if (device && typeof device.token === "string") return device.token;
    if (typeof payload.device_token === "string") return payload.device_token;
    if (typeof payload.worker_token === "string") return payload.worker_token;
    return "";
  }

  function bindLink() {
    var form = document.getElementById("link-form");
    if (!form) return;
    var alert = document.getElementById("form-alert");
    MR.session().then(function (session) {
      var gate = document.getElementById("logged-out");
      var app = document.getElementById("logged-in");
      if (session.state !== "in") {
        if (gate) gate.hidden = false;
        if (app) app.hidden = true;
        var note = document.getElementById("gate-note");
        if (note && session.state === "down") {
          note.textContent = "Could not reach the account service, so a device cannot be linked yet.";
        }
        return;
      }
      if (gate) gate.hidden = true;
      if (app) app.hidden = false;
    });
    var codeInput = form.device_code;
    var hostNote = document.getElementById("device-host");
    var fromUrl = new URLSearchParams(window.location.search).get("code") || "";
    if (fromUrl && codeInput) {
      codeInput.value = fromUrl;
      request(PATHS.device(fromUrl)).then(function (result) {
        if (result.res.ok && hostNote && result.payload && result.payload.hostname) {
          hostNote.textContent = "Computer asking to link: " + String(result.payload.hostname).slice(0, 80);
          hostNote.hidden = false;
        }
      }).catch(function () {});
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      show(alert, "error", "");
      var button = form.querySelector("button[type=submit]");
      busy(button, true, "Linking…");
      var code = codeInput.value.trim();
      request(PATHS.approve, {
        method: "POST",
        body: { code: code }
      }).then(function (result) {
        if (!result.res.ok) {
          var msg = result.res.status === 404 || result.res.status === 410
            ? "That link code was not found or has expired. Start the link again on the worker."
            : fallbackFor(result.res.status, "link");
          show(alert, "error", safeMessage(result.payload, msg));
          return;
        }
        var host = result.payload && result.payload.hostname;
        show(alert, "ok", (host ? String(host).slice(0, 80) + " is linked." : "This computer is linked.") +
          " The worker saves its own key and starts on its own. You can revoke it from My account.");
        form.reset();
      }).catch(function (err) {
        show(alert, "error", err.message);
      }).then(function () {
        busy(button, false);
      });
    });
    var copy = document.getElementById("copy-token");
    if (copy) {
      copy.addEventListener("click", function () {
        var input = document.getElementById("device-token");
        var value = input ? input.value : "";
        if (!value) return;
        var done = function () {
          copy.textContent = "Copied";
          window.setTimeout(function () { copy.textContent = "Copy"; }, 2000);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(value).then(done).catch(function () {
            input.focus();
            input.select();
            copy.textContent = "Select the key and copy it";
          });
        } else {
          input.focus();
          input.select();
        }
      });
    }
  }

  function boot() {
    var which = page();
    if (which === "signup" || which === "login") goAccountIfSession();
    if (which === "signup") bindSignup();
    if (which === "login") bindLogin();
    if (which === "account") bindAccount();
    if (which === "link") bindLink();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
