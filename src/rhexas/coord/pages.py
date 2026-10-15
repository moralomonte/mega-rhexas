"""Small account pages. Same dark screen as the status page, including the spinning mark.

These exist so signup and device linking work before the public site is up.
The public site on https://megarhexas.com calls the JSON API instead.
"""
from __future__ import annotations

import re
from pathlib import Path

_MARK: str | None = None


def _esc(value) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _mark() -> str:
    global _MARK
    if _MARK is None:
        raw = (Path(__file__).with_name("favicon.svg")).read_text(encoding="utf-8")
        _MARK = re.sub(
            r"<svg\b[^>]*>",
            '<svg class="trisk spin" aria-hidden="true" viewBox="-20 -20 721 721" width="42" height="42">',
            raw,
            count=1,
        )
    return _MARK


def render(title: str, body: str, *, wide: bool = False) -> bytes:
    width = "760px" if wide else "440px"
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{_esc(title)}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root{{--bg:#05060f;--glass:rgba(255,255,255,.05);--bd:rgba(255,255,255,.1);--tx:#e7ecff;--mut:#8b93b8;--c1:#7c5cff;--c2:#22d3ee;--ok:#34d399;--warn:#fbbf24;--err:#f87171}}
*{{box-sizing:border-box}}
html,body{{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 "Segoe UI Variable","Segoe UI",Inter,system-ui,sans-serif}}
#stars{{position:fixed;inset:0;z-index:-1}}
.wrap{{max-width:{width};margin:0 auto;padding:28px 18px 48px}}
.logo{{display:flex;align-items:center;gap:12px;margin-bottom:18px}}
.trisk{{width:42px;height:42px;flex:none;filter:drop-shadow(0 0 8px #818cf866);transition:filter .4s}}
.logo:hover .trisk,.trisk.spin{{animation:trspin 12s linear infinite}}
.logo:hover .trisk{{filter:drop-shadow(0 0 14px #a5b4fcaa)}}
@keyframes trspin{{to{{transform:rotate(360deg)}}}}
@media (prefers-reduced-motion:reduce){{.trisk{{animation:none!important}}}}
h1{{font-size:20px;margin:0;letter-spacing:.2px}}
h1 small{{display:block;font-size:12px;color:var(--mut);font-weight:400}}
.card{{background:linear-gradient(165deg,rgba(255,255,255,.07),rgba(255,255,255,.02));border:1px solid var(--bd);border-radius:18px;padding:18px 18px 20px;box-shadow:0 10px 40px rgba(0,0,0,.28)}}
h2{{margin:0 0 8px;font-size:18px}}
.note,.muted{{color:var(--mut);font-size:13px}}
label{{display:block;margin:14px 0 6px;color:var(--mut);font-size:12px}}
input{{width:100%;background:#0b0d1c;border:1px solid var(--bd);color:var(--tx);border-radius:10px;padding:10px 12px;font:inherit}}
button,.linkbtn{{display:inline-block;background:var(--glass);border:1px solid var(--bd);color:var(--tx);border-radius:9px;padding:8px 12px;font:inherit;font-size:13px;cursor:pointer;text-decoration:none}}
button.primary{{background:linear-gradient(90deg,#7c5cff,#22d3ee);color:#071018;border:0;font-weight:650}}
.row{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:16px}}
.err{{color:var(--err);min-height:1.2em;font-size:13px}}
.ok{{color:var(--ok)}}
a{{color:var(--c2)}}
.host{{font-size:18px;margin:12px 0}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th{{text-align:left;color:var(--mut);font-weight:600;font-size:11px;letter-spacing:.6px;text-transform:uppercase}}
td,th{{padding:7px 6px;border-bottom:1px solid rgba(255,255,255,.06);vertical-align:middle}}
footer{{margin-top:16px;color:var(--mut);font-size:12px}}
.stats{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}
.stat b{{display:block;font-size:22px;font-variant-numeric:tabular-nums}}
.stat span{{font-size:11px;color:var(--mut);letter-spacing:.6px;text-transform:uppercase}}
</style>
</head>
<body>
<canvas id="stars"></canvas>
<div class="wrap">
<a class="logo" href="/account">
{_mark()}
<div><h1>Mega Rhexas <small>{_esc(title)}</small></h1></div>
</a>
{body}
<footer>Batches are kept when two different accounts agree. Pending credit is not a coin.</footer>
</div>
<script>
(() => {{
  const c = document.getElementById('stars');
  if (!c) return;
  const x = c.getContext('2d');
  let S = [];
  function rs(){{c.width = innerWidth; c.height = innerHeight; S = Array.from({{length: 120}}, () => ({{x: Math.random()*c.width, y: Math.random()*c.height, r: Math.random()*1.2, a: Math.random(), s: .002+Math.random()*.01}}))}}
  rs(); addEventListener('resize', rs);
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  function frame(){{
    const g = x.createRadialGradient(c.width*.8, 0, 0, c.width*.8, 0, c.width);
    g.addColorStop(0,'#1a1040'); g.addColorStop(.55,'#070818'); g.addColorStop(1,'#04050c');
    x.fillStyle = g; x.fillRect(0,0,c.width,c.height);
    for (const s of S){{if(!reduce) s.a += s.s; x.globalAlpha = .25+.7*Math.abs(Math.sin(s.a)); x.fillStyle = '#fff'; x.beginPath(); x.arc(s.x,s.y,s.r,0,7); x.fill()}}
    x.globalAlpha = 1;
    if (!reduce) requestAnimationFrame(frame);
  }}
  frame();
}})();
</script>
</body>
</html>
"""
    return html.encode("utf-8")


def signup_page(*, error: str = "", invite: bool = True, email: str = "", display_name: str = "",
                invite_code: str = "", next_path: str = "/account") -> bytes:
    invite_field = ""
    if invite:
        invite_field = f"""
<label for="invite">Invite code</label>
<input id="invite" name="invite_code" autocomplete="off" required value="{_esc(invite_code)}">
"""
    body = f"""
<section class="card">
<h2>Create an account</h2>
<p class="note">Then link the computer that will run the search. No token to copy.</p>
<form method="post" action="/signup">
<input type="hidden" name="next" value="{_esc(next_path)}">
<label for="email">Email</label>
<input id="email" name="email" type="email" autocomplete="email" required value="{_esc(email)}">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="new-password" required minlength="10">
<label for="name">Display name</label>
<input id="name" name="display_name" autocomplete="nickname" required value="{_esc(display_name)}">
{invite_field}
<p class="err">{_esc(error)}</p>
<div class="row"><button class="primary" type="submit">Sign up</button>
<a href="/login?next={_esc(next_path)}">Log in</a></div>
</form>
</section>
"""
    return render("Sign up", body)


def login_page(*, error: str = "", email: str = "", next_path: str = "/account") -> bytes:
    body = f"""
<section class="card">
<h2>Log in</h2>
<p class="note">This is your Mega Rhexas account, not the admin token.</p>
<form method="post" action="/account/login">
<input type="hidden" name="next" value="{_esc(next_path)}">
<label for="email">Email</label>
<input id="email" name="email" type="email" autocomplete="username" required value="{_esc(email)}">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<p class="err">{_esc(error)}</p>
<div class="row"><button class="primary" type="submit">Log in</button>
<a href="/signup?next={_esc(next_path)}">Sign up</a></div>
</form>
</section>
"""
    return render("Log in", body)


def link_page(*, code: str, hostname: str, logged_in: bool, display_name: str = "", error: str = "") -> bytes:
    next_path = "/link?code=" + code
    if error:
        inner = f"<p class='err'>{_esc(error)}</p>"
    elif not logged_in:
        inner = f"""
<p class="host">Link this computer: <b>{_esc(hostname)}</b></p>
<p class="note">Log in or create an account, then approve this computer. Code {_esc(code)}.</p>
<div class="row">
<a class="linkbtn primary" href="/login?next={_esc(next_path)}">Log in</a>
<a class="linkbtn" href="/signup?next={_esc(next_path)}">Sign up</a>
</div>
"""
    else:
        inner = f"""
<p class="host">Link this computer: <b>{_esc(hostname)}</b></p>
<p class="note">This links it to <b>{_esc(display_name)}</b>. It can then claim batches for your account. Code {_esc(code)}.</p>
<div class="row">
<form method="post" action="/link/approve">
<input type="hidden" name="code" value="{_esc(code)}">
<button class="primary" type="submit">Approve</button>
</form>
<form method="post" action="/link/deny">
<input type="hidden" name="code" value="{_esc(code)}">
<button type="submit">Deny</button>
</form>
</div>
"""
    body = f"<section class='card'><h2>Link a computer</h2>{inner}</section>"
    return render("Link a computer", body)


def account_page(*, account: dict, devices: list[dict], stats: dict, note: str = "",
                 verify_offered: bool = False) -> bytes:
    verified = "verified" if account.get("email_verified") else "not verified yet"
    banner = f"<p class='ok'>{_esc(note)}</p>" if note else ""
    resend = ""
    if verify_offered and not account.get("email_verified"):
        resend = """
<form method="post" action="/account/verify/resend"><button type="submit">Send verification email</button></form>
"""
    device_rows = []
    if not devices:
        device_rows.append("<tr><td colspan='4' class='muted'>No computers linked yet. Run <code>rhexas worker --login</code> on the one that will search.</td></tr>")
    for device in devices:
        label = device.get("hostname") or device.get("name") or "device"
        state = "revoked" if device.get("revoked_at") else "active"
        revoke = ""
        if not device.get("revoked_at"):
            revoke = (
                f"<form method='post' action='/account/devices/{int(device['id'])}/revoke'>"
                "<button type='submit'>Revoke</button></form>"
            )
        device_rows.append(
            "<tr>"
            f"<td>{_esc(label)}</td>"
            f"<td>{_esc(device.get('kind') or '')}</td>"
            f"<td>{_esc(state)}</td>"
            f"<td>{revoke}</td>"
            "</tr>"
        )
    body = f"""
<section class="card">
<h2>{_esc(account.get('display_name'))}</h2>
{banner}
<p class="note">{_esc(account.get('email') or '')} · {_esc(verified)}</p>
{resend}
<div class="stats">
<div class="stat"><span>Stars verified</span><b>{int(stats.get('stars_verified') or 0)}</b></div>
<div class="stat"><span>Stars pending</span><b>{int(stats.get('stars_pending') or 0)}</b></div>
<div class="stat"><span>Batches verified</span><b>{int(stats.get('batches_verified') or 0)}</b></div>
<div class="stat"><span>Batches pending</span><b>{int(stats.get('batches_pending') or 0)}</b></div>
</div>
<p class="note">Pending until another account agrees on the same batch. Revoking a device drops its open lease. There are no coins.</p>
<h2>Computers</h2>
<table>
<tr><th>Computer</th><th></th><th></th><th></th></tr>
{''.join(device_rows)}
</table>
<div class="row">
<form method="post" action="/account/logout"><button type="submit">Log out</button></form>
</div>
</section>
"""
    return render("Your account", body, wide=True)


def message_page(title: str, text: str, *, error: bool = False) -> bytes:
    kind = "err" if error else "note"
    body = f"<section class='card'><h2>{_esc(title)}</h2><p class='{kind}'>{_esc(text)}</p></section>"
    return render(title, body)
