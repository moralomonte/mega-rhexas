"""The public site is static files. These checks stay offline."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
PAGES = [
    "index.html",
    "how-it-works.html",
    "leaderboard.html",
    "signup.html",
    "login.html",
    "account.html",
    "link-device.html",
    "404.html",
]

NASA = "Mega Rhexas reads public NASA TESS data. It is not part of NASA, and NASA has not endorsed it."
INVITE = "The beta is invite-only for now."
COINS = "There are no coins and no crypto."
UNVERIFIED = "unverified"


def _html():
    return {name: (SITE / name).read_text(encoding="utf-8") for name in PAGES}


def test_pages_exist_and_share_the_rules():
    pages = _html()
    for name, text in pages.items():
        assert 'lang="en"' in text
        assert 'name="viewport"' in text
        assert "/css/site.css" in text
        assert "/img/triskelion.svg" in text
        assert NASA in text, name
        assert INVITE in text, name
        assert COINS in text, name
        assert UNVERIFIED in text, name
        assert "bitcoin" not in text.lower()
        assert "ethereum" not in text.lower()
        assert "blockchain" not in text.lower()
        assert "cryptocurrency" not in text.lower()
        assert "we discovered" not in text.lower()
        assert "planet discovered" not in text.lower()
        assert "nasa official" not in text.lower()


def test_home_calls_to_action_and_stats_contract():
    home = (SITE / "index.html").read_text(encoding="utf-8")
    stats = (SITE / "js" / "stats.js").read_text(encoding="utf-8")
    home_js = (SITE / "js" / "home.js").read_text(encoding="utf-8")
    assert "Join the search" in home
    assert ">Download<" in home
    assert "https://github.com/moralomonte/mega-rhexas/releases/latest" in home
    assert "/api/v1/public/stats" in stats
    assert "SAMPLE_STATS" in stats
    assert "localeCompare" in stats
    assert "display_name" in stats
    assert "prefers-reduced-motion" in (SITE / "css" / "site.css").read_text(encoding="utf-8")
    assert "trspin" in (SITE / "css" / "site.css").read_text(encoding="utf-8")
    assert "arc-ok" in home and "arc-wip" in home_js


def test_how_it_works_is_plain_and_has_the_placeholder():
    text = (SITE / "how-it-works.html").read_text(encoding="utf-8")
    for heading in (
        "A transit",
        "What your computer does",
        "Why two people check each batch",
        "What happens to a candidate",
        "How much it uses your computer",
        "Privacy",
    ):
        assert heading in text
    assert "Technical details (coming soon)" in text
    assert ">Eco<" in text and ">Live<" in text and ">Turbo<" in text
    assert "deletes the big files" in text.lower() or "Delete the big files" in text


def test_account_pages_call_the_coordinator_contract():
    js = (SITE / "js" / "account.js").read_text(encoding="utf-8")
    for path in (
        "/api/v1/account/signup",
        "/api/v1/account/login",
        "/api/v1/account/logout",
        "/api/v1/account/me",
        "/api/v1/account/devices",
        "/api/v1/account/stats",
        "/revoke",
    ):
        assert path in js
    assert 'credentials: "include"' in js
    assert "TODO(account-api)" in js
    assert "invite_code" in js
    signup = (SITE / "signup.html").read_text(encoding="utf-8")
    assert 'data-page="signup"' in signup
    assert (SITE / "link-device.html").read_text(encoding="utf-8").count("Link a device") >= 1
    assert 'data-page="account"' in (SITE / "account.html").read_text(encoding="utf-8")


def test_caddy_serves_the_apex_from_site():
    snippet = (ROOT / "deploy" / "Caddyfile.site").read_text(encoding="utf-8")
    host = (ROOT / "deploy" / "Caddyfile.host").read_text(encoding="utf-8")
    assert "megarhexas.com" in snippet
    assert "www.megarhexas.com" in snippet
    assert "/opt/mega-rhexas/site" in snippet
    assert "try_files {path} {path}.html" in snippet
    assert "import /opt/mega-rhexas/deploy/Caddyfile.site" in host
    assert "reverse_proxy 127.0.0.1:8787" in host
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "deploy/Caddyfile.site" in deploy
    assert "https://megarhexas.com" in deploy
