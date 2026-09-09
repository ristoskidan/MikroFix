"""Ozko omejen javni iFixit EU cenovni vir za pulled module."""

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
from typing import Callable


IFIXIT_BASE = "https://www.ifixit.com"
MAX_PAGE_BYTES = 3_000_000


class IFixitPriceError(RuntimeError):
    pass


@dataclass(frozen=True)
class IFixitOffer:
    title: str
    price_eur: float
    url: str


def pro_profile_path(inventory_db: Path | str) -> Path:
    """Namenski Firefox profil je ločen po produkcijski/sandbox podatkovni mapi."""
    root = Path(inventory_db).resolve().parent.parent
    return root / "secrets" / "ifixit_pro_firefox"


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
            )) + '\n',
            encoding="utf-8",
        )
        prefs.chmod(0o600)
    return profile


def launch_pro_login(profile_path: Path | str) -> subprocess.Popen:
    firefox = shutil.which("firefox")
    if not firefox:
        raise IFixitPriceError("Firefox ni nameščen; iFixit Pro prijave ni mogoče odpreti.")
    profile = _prepare_profile(profile_path)
    return subprocess.Popen(
        [
            firefox, "--no-remote", "--profile", str(profile), "--new-window",
            "https://www.ifixit.com/Login?redirect=%2FUser",
        ],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )


def _cookie_jar(profile_path: Path | str) -> http.cookiejar.CookieJar:
    profile = Path(profile_path).resolve()
    database = profile / "cookies.sqlite"
    if not database.is_file():
        return http.cookiejar.CookieJar()
    jar = http.cookiejar.CookieJar()
    connection = sqlite3.connect(
        database.as_uri() + "?mode=ro", uri=True, timeout=3
    )
    try:
        rows = connection.execute(
            """
            SELECT host,path,isSecure,expiry,name,value
            FROM moz_cookies
            WHERE host IN ('ifixit.com','.ifixit.com','www.ifixit.com','.www.ifixit.com')
            """
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
            discard=expires is None, comment=None, comment_url=None,
            rest={}, rfc2109=False,
        ))
    return jar


def _authenticated_open(
    url: str, profile_path: Path | str, *, timeout: float = 10.0,
) -> tuple[str, str]:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_cookie_jar(profile_path))
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MikroFix/30 iFixit-Pro-price-check",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-EU,en;q=0.9",
        },
    )
    with opener.open(request, timeout=timeout) as response:
        body = response.read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise IFixitPriceError("iFixit stran je nepričakovano velika.")
        return response.geturl(), body.decode(
            response.headers.get_content_charset() or "utf-8", "replace"
        )


def pro_session_connected(profile_path: Path | str, *, timeout: float = 8.0) -> bool:
    if not list(_cookie_jar(profile_path)):
        return False
    try:
        final_url, _document = _authenticated_open(
            "https://www.ifixit.com/User", profile_path, timeout=timeout
        )
    except Exception:
        return False
    return urllib.parse.urlparse(final_url).path.casefold() != "/login"


def disconnect_pro_session(profile_path: Path | str) -> None:
    """Odstrani samo prijavno stanje iz namenskega, znanega iFixit profila."""
    profile = Path(profile_path).resolve()
    if profile.name != "ifixit_pro_firefox" or profile.parent.name != "secrets":
        raise IFixitPriceError("Neveljavna pot namenskega iFixit profila.")
    for name in ("cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm"):
        target = profile / name
        if target.is_file():
            target.unlink()
    storage = profile / "storage" / "default"
    if storage.is_dir():
        for target in storage.iterdir():
            if "ifixit.com" in target.name.casefold() and target.is_dir():
                shutil.rmtree(target)


def parts_url(model: str) -> str:
    clean = re.sub(r"\s+", " ", str(model or "").strip())
    clean = re.sub(r"^Apple\s+", "", clean, flags=re.I)
    if not clean:
        raise IFixitPriceError("Manjka model naprave za iFixit primerjavo.")
    slug = urllib.parse.quote(clean.replace(" ", "_"), safe="_")
    return f"{IFIXIT_BASE}/en-eu/Parts/{slug}"


def fetch_page(url: str, *, timeout: float = 10.0) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MikroFix/30 pulled-part-price-check",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-EU,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise IFixitPriceError("iFixit stran je nepričakovano velika.")
        return body.decode(response.headers.get_content_charset() or "utf-8", "replace")


def parse_offers(document: str) -> list[IFixitOffer]:
    offers: list[IFixitOffer] = []
    seen: set[tuple[str, str]] = set()
    for article in re.findall(r"<article\b.*?</article>", document, flags=re.I | re.S):
        title_match = re.search(r"<h3\b[^>]*>(.*?)</h3>", article, flags=re.I | re.S)
        href_match = re.search(r'href=["\']([^"\']*/en-eu/products/[^"\']+)["\']', article, flags=re.I)
        price_match = re.search(
            r'data-testid=["\']price["\'][^>]*>.*?<span[^>]*>\s*€\s*([0-9.,]+)',
            article, flags=re.I | re.S,
        )
        if not (title_match and href_match and price_match):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        href = html.unescape(href_match.group(1))
        try:
            price = float(price_match.group(1).replace(".", "").replace(",", "."))
            # iFixit EU trenutno uporablja decimalno piko; zgornji evropski zapis
            # bi 1.234,56 pravilno pretvoril, navadni 136.95 pa ne.
            if "." in price_match.group(1) and "," not in price_match.group(1):
                price = float(price_match.group(1))
        except ValueError:
            continue
        url = urllib.parse.urljoin(IFIXIT_BASE, href)
        key = (title.casefold(), url)
        if title and price > 0 and key not in seen:
            offers.append(IFixitOffer(title, round(price, 2), url))
            seen.add(key)
    if not offers:
        raise IFixitPriceError("Na iFixit strani ni bilo mogoče prebrati cen delov.")
    return offers


MODULE_TERMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "DISPLAY": (
        ("display assembly", "oled screen", "lcd screen", "screen"),
        ("protector", "adhesive", "test cable", "screen cable"),
    ),
    "BATTERY": (
        ("battery",),
        ("adhesive", "connector", "cable", "case", "tester"),
    ),
    "REAR_CAMERA": (
        ("rear camera assembly", "rear camera"),
        ("lens", "cover", "bezel", "glass", "test cable", "bracket"),
    ),
    "FRONT_CAMERA": (
        ("front camera assembly", "front camera and sensor cable", "front camera", "front sensor cable"),
        ("test cable", "bracket", "lens", "cover"),
    ),
    "EARPIECE": (
        ("earpiece speaker and sensor assembly", "earpiece speaker", "ear speaker"),
        ("mesh", "adhesive", "gasket", "foam"),
    ),
    "CHARGING_PORT": (
        (
            "usb-c connector assembly", "usb-c charge port", "usb-c port",
            "lightning connector assembly", "lightning port", "dock connector",
            "charge port", "charging port",
        ),
        ("adhesive", "gasket", "test cable", "protector"),
    ),
    "LOUDSPEAKER": (
        ("loudspeaker assembly", "loudspeaker"),
        ("adhesive", "gasket", "mesh", "foam", "connector"),
    ),
    "TAPTIC": (
        ("taptic engine", "vibration motor", "vibrator"),
        ("adhesive", "bracket"),
    ),
    "HOUSING": (
        ("rear housing assembly", "rear housing", "rear case assembly", "rear case", "midframe"),
        ("adhesive", "button set", "glass", "cover"),
    ),
    "LOGIC_BOARD": (
        ("logic board assembly", "logic board", "motherboard"),
        ("test cable", "shield", "bracket"),
    ),
    "WIRELESS_CHARGING": (
        ("wireless charging antenna assembly", "wireless charging coil", "wireless charging antenna"),
        ("adhesive", "test cable"),
    ),
}


def match_modules(offers: list[IFixitOffer]) -> dict[str, IFixitOffer]:
    matched: dict[str, IFixitOffer] = {}
    for module_code, (wanted, excluded) in MODULE_TERMS.items():
        candidates = []
        for offer in offers:
            title = offer.title.casefold()
            if any(term in title for term in excluded):
                continue
            ranks = [index for index, term in enumerate(wanted) if term in title]
            if ranks:
                candidates.append((min(ranks), offer))
        if candidates:
            # Najprej izberemo najbolj konkretno poimenovanje, cena je šele
            # deterministični tie-breaker med enako natančnimi zadetki.
            matched[module_code] = min(
                candidates, key=lambda candidate: (candidate[0], candidate[1].price_eur)
            )[1]
    return matched


def fetch_module_prices(
    model: str, *, timeout: float = 10.0,
    fetcher: Callable[..., str] | None = None,
) -> tuple[str, dict[str, IFixitOffer]]:
    url = parts_url(model)
    document = (fetcher or fetch_page)(url, timeout=timeout)
    return url, match_modules(parse_offers(document))


def fetch_pro_module_prices(
    model: str, profile_path: Path | str, *, timeout: float = 10.0,
) -> tuple[str, dict[str, IFixitOffer]]:
    if not pro_session_connected(profile_path, timeout=timeout):
        raise IFixitPriceError("iFixit Pro seja ni prijavljena ali je potekla.")
    url = parts_url(model)
    _final_url, document = _authenticated_open(url, profile_path, timeout=timeout)
    return url, match_modules(parse_offers(document))
