"""FixShop javni in prijavljeni cenik za primerjavo donorskih modulov."""

from __future__ import annotations

import html
import http.cookiejar
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


FIXSHOP_BASE = "https://fixshop.si"
MAX_PAGE_BYTES = 3_000_000


class FixShopPriceError(RuntimeError):
    pass


@dataclass(frozen=True)
class FixShopOffer:
    title: str
    price_eur: float
    url: str


def pro_profile_path(inventory_db: Path | str) -> Path:
    root = Path(inventory_db).resolve().parent.parent
    return root / "secrets" / "fixshop_pro_firefox"


def _prepare_profile(profile_path: Path | str) -> Path:
    profile = Path(profile_path).resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    prefs = profile / "user.js"
    if not prefs.exists():
        prefs.write_text(
            '\n'.join((
                'user_pref("browser.shell.checkDefaultBrowser", false);',
                'user_pref("browser.startup.page", 0);',
                'user_pref("datareporting.healthreport.uploadEnabled", false);',
                'user_pref("toolkit.telemetry.enabled", false);',
                'user_pref("signon.rememberSignons", false);',
            )) + '\n', encoding="utf-8",
        )
        prefs.chmod(0o600)
    return profile


def launch_pro_login(profile_path: Path | str) -> subprocess.Popen:
    firefox = shutil.which("firefox")
    if not firefox:
        raise FixShopPriceError("Firefox ni nameščen; FixShop Pro prijave ni mogoče odpreti.")
    profile = _prepare_profile(profile_path)
    return subprocess.Popen(
        [firefox, "--no-remote", "--profile", str(profile), "--new-window",
         "https://fixshop.si/racun/prijava/"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )


def _cookie_jar(profile_path: Path | str) -> http.cookiejar.CookieJar:
    database = Path(profile_path).resolve() / "cookies.sqlite"
    jar = http.cookiejar.CookieJar()
    if not database.is_file():
        return jar
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        rows = connection.execute(
            """SELECT host,path,isSecure,expiry,name,value FROM moz_cookies
               WHERE host IN ('fixshop.si','.fixshop.si','www.fixshop.si','.www.fixshop.si')"""
        ).fetchall()
    finally:
        connection.close()
    now = int(time.time())
    for domain, path, secure, expiry, name, value in rows:
        expires = int(expiry or 0) or None
        if expires is not None and expires < now:
            continue
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=str(name), value=str(value), port=None,
            port_specified=False, domain=str(domain),
            domain_specified=str(domain).startswith("."),
            domain_initial_dot=str(domain).startswith("."), path=str(path or "/"),
            path_specified=True, secure=bool(secure), expires=expires,
            discard=expires is None, comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    return jar


def _open(url: str, *, profile_path: Path | str | None = None, timeout: float = 10) -> tuple[str, str]:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_cookie_jar(profile_path))
    ) if profile_path else urllib.request.build_opener()
    request = urllib.request.Request(url, headers={
        "User-Agent": "MikroFix/30 FixShop-price-check",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "sl,en;q=0.8",
    })
    with opener.open(request, timeout=timeout) as response:
        body = response.read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise FixShopPriceError("FixShop stran je nepričakovano velika.")
        return response.geturl(), body.decode(
            response.headers.get_content_charset() or "utf-8", "replace"
        )


def pro_session_connected(profile_path: Path | str, *, timeout: float = 8) -> bool:
    if not list(_cookie_jar(profile_path)):
        return False
    try:
        final_url, _ = _open(
            "https://fixshop.si/racun/", profile_path=profile_path, timeout=timeout
        )
    except Exception:
        return False
    return "/racun/prijava/" not in urllib.parse.urlparse(final_url).path.casefold()


def disconnect_pro_session(profile_path: Path | str) -> None:
    profile = Path(profile_path).resolve()
    if profile.name != "fixshop_pro_firefox" or profile.parent.name != "secrets":
        raise FixShopPriceError("Neveljavna pot namenskega FixShop profila.")
    for name in ("cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm"):
        target = profile / name
        if target.is_file():
            target.unlink()
    storage = profile / "storage" / "default"
    if storage.is_dir():
        for target in storage.iterdir():
            if "fixshop.si" in target.name.casefold() and target.is_dir():
                shutil.rmtree(target)


def parts_url(model: str) -> str:
    clean = re.sub(r"\s+", " ", str(model or "").strip())
    clean = re.sub(r"^Apple\s+", "", clean, flags=re.I)
    if not clean:
        raise FixShopPriceError("Manjka model naprave za FixShop primerjavo.")
    slug = re.sub(r"[^a-z0-9]+", "-", clean.casefold()).strip("-")
    return f"{FIXSHOP_BASE}/rezervni-deli-apple-iphone-apple-{slug}/"


def parse_offers(document: str) -> list[FixShopOffer]:
    offers: list[FixShopOffer] = []
    seen: set[str] = set()
    cards = re.split(r'<div[^>]+class="[^"]*product-card-wrapper', document, flags=re.I)[1:]
    for card in cards:
        card = card[:12000]
        link = re.search(
            r'<a[^>]+href="([^"]+)"[^>]+title="([^"]+)"[^>]+class="[^"]*product-card-image',
            card, flags=re.I | re.S,
        )
        price = re.search(
            r'<p[^>]+class="[^"]*font-bold\s+text-p-small[^"]*"[^>]*>\s*([0-9.,]+)\s*€',
            card, flags=re.I | re.S,
        )
        if not (link and price):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip()
        url = urllib.parse.urljoin(FIXSHOP_BASE, html.unescape(link.group(1)))
        raw = price.group(1).strip()
        try:
            amount = float(raw.replace(".", "").replace(",", "."))
        except ValueError:
            continue
        if title and amount > 0 and url not in seen:
            offers.append(FixShopOffer(title, round(amount, 2), url))
            seen.add(url)
    if not offers:
        raise FixShopPriceError("Na FixShop strani ni bilo mogoče prebrati cen delov.")
    return offers


MODULE_TERMS = {
    "DISPLAY": (("zaslon", "lcd", "oled"), ("lepilo", "testni kabel")),
    "BATTERY": (("baterija",), ("lepilo", "konektor", "tester")),
    "REAR_CAMERA": (("zadnja kamera",), ("steklo", "leča", "okvir", "testni")),
    "FRONT_CAMERA": (("sprednja kamera z ir kamero", "sprednja kamera"), ("steklo", "testni")),
    "EARPIECE": (("svetlobni senzor + slušalka", "slušalka + flex", "slušalka"), ("mrežica", "lepilo")),
    "CHARGING_PORT": (("konektor za polnjenje + flex", "konektor za polnjenje", "polnilni konektor"), ("ic ", "čip", "tester")),
    "LOUDSPEAKER": (("zvočnik",), ("slušalka", "mrežica", "lepilo")),
    "TAPTIC": (("taptic engine", "vibracijski motor", "vibrator"), ("lepilo",)),
    "HOUSING": (("ohišje", "sredinski okvir"), ("steklo", "lepilo", "ovitek")),
    "LOGIC_BOARD": (("matična plošča", "logic board", "motherboard"), ("čip", "nosilec")),
    "WIRELESS_CHARGING": (("brezžično polnjenje", "wireless charging"), ("lepilo",)),
}


def match_modules(offers: list[FixShopOffer]) -> dict[str, FixShopOffer]:
    matched: dict[str, FixShopOffer] = {}
    for code, (wanted, excluded) in MODULE_TERMS.items():
        candidates = []
        for offer in offers:
            title = offer.title.casefold()
            if any(term in title for term in excluded):
                continue
            ranks = [index for index, term in enumerate(wanted) if term in title]
            if ranks:
                candidates.append((min(ranks), offer))
        if candidates:
            matched[code] = min(
                candidates, key=lambda candidate: (candidate[0], candidate[1].price_eur)
            )[1]
    return matched


def fetch_module_prices(
    model: str, *, profile_path: Path | str | None = None, timeout: float = 10,
) -> tuple[str, dict[str, FixShopOffer]]:
    url = parts_url(model)
    offers: list[FixShopOffer] = []
    seen: set[str] = set()
    # Omejen pregled strani modela; prekine se ob prazni ali ponovljeni strani.
    for page in range(1, 5):
        page_url = url if page == 1 else f"{url}?p={page}"
        _final, document = _open(page_url, profile_path=profile_path, timeout=timeout)
        current = parse_offers(document)
        added = 0
        for offer in current:
            if offer.url not in seen:
                offers.append(offer)
                seen.add(offer.url)
                added += 1
        if added == 0 or len(current) < 20:
            break
    return url, match_modules(offers)
