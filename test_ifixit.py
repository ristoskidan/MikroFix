import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mikrofix_ifixit import (
    _cookie_jar,
    disconnect_pro_session,
    match_modules,
    parse_offers,
    parts_url,
    pro_profile_path,
    pro_session_connected,
)


class IFixitPriceTests(unittest.TestCase):
    def test_model_url_uses_eu_catalog(self):
        self.assertEqual(
            parts_url("Apple iPhone 15 Pro"),
            "https://www.ifixit.com/en-eu/Parts/iPhone_15_Pro",
        )

    def test_parser_matches_parts_and_rejects_accessories(self):
        document = """
        <article><a href="/en-eu/products/iphone-15-pro-screen"><h3>iPhone 15 Pro Screen</h3></a>
        <div data-testid="price"><span>€119.95</span></div></article>
        <article><a href="/en-eu/products/adhesive"><h3>iPhone 15 Pro Screen Adhesive</h3></a>
        <div data-testid="price"><span>€4.95</span></div></article>
        <article><a href="/en-eu/products/camera"><h3>iPhone 15 Pro Rear Camera Assembly</h3></a>
        <div data-testid="price"><span>€136,95</span></div></article>
        <article><a href="/en-eu/products/test-camera"><h3>iPhone X Test Cables for Rear Camera</h3></a>
        <div data-testid="price"><span>€12.95</span></div></article>
        <article><a href="/en-eu/products/battery-connector"><h3>iPhone Battery Connector</h3></a>
        <div data-testid="price"><span>€9.95</span></div></article>
        <article><a href="/en-eu/products/battery"><h3>iPhone 15 Pro Battery</h3></a>
        <div data-testid="price"><span>€49.95</span></div></article>
        <article><a href="/en-eu/products/lightning"><h3>iPhone 14 Pro Lightning Connector Assembly</h3></a>
        <div data-testid="price"><span>€89.95</span></div></article>
        <article><a href="/en-eu/products/loudspeaker-gasket"><h3>iPhone Loudspeaker Adhesive Gasket</h3></a>
        <div data-testid="price"><span>€0.95</span></div></article>
        <article><a href="/en-eu/products/loudspeaker"><h3>iPhone 15 Pro Loudspeaker</h3></a>
        <div data-testid="price"><span>€18.95</span></div></article>
        <article><a href="/en-eu/products/vibrator"><h3>iPhone 14 Pro Vibrator</h3></a>
        <div data-testid="price"><span>€4.95</span></div></article>
        """
        matches = match_modules(parse_offers(document))
        self.assertEqual(matches["DISPLAY"].price_eur, 119.95)
        self.assertEqual(matches["REAR_CAMERA"].price_eur, 136.95)
        self.assertEqual(matches["BATTERY"].price_eur, 49.95)
        self.assertEqual(matches["CHARGING_PORT"].price_eur, 89.95)
        self.assertEqual(matches["LOUDSPEAKER"].price_eur, 18.95)
        self.assertEqual(matches["TAPTIC"].price_eur, 4.95)
        self.assertNotIn("adhesive", matches["DISPLAY"].title.casefold())

    def test_pro_profile_is_isolated_inside_selected_data_root(self):
        path = pro_profile_path("/data/sandbox/db/mikrofix_inventory.db")
        self.assertEqual(path, Path("/data/sandbox/secrets/ifixit_pro_firefox"))

    def test_only_ifixit_cookies_are_loaded_and_disconnect_removes_them(self):
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw) / "secrets" / "ifixit_pro_firefox"
            profile.mkdir(parents=True)
            c = sqlite3.connect(profile / "cookies.sqlite")
            c.execute(
                "CREATE TABLE moz_cookies(host TEXT,path TEXT,isSecure INTEGER,"
                "expiry INTEGER,name TEXT,value TEXT)"
            )
            c.execute(
                "INSERT INTO moz_cookies VALUES('.ifixit.com','/',1,4102444800,'session','secret')"
            )
            c.execute(
                "INSERT INTO moz_cookies VALUES('.example.com','/',1,4102444800,'other','ignore')"
            )
            c.commit(); c.close()
            cookies = list(_cookie_jar(profile))
            self.assertEqual([(x.domain, x.name) for x in cookies], [('.ifixit.com', 'session')])
            disconnect_pro_session(profile)
            self.assertFalse((profile / "cookies.sqlite").exists())

    def test_login_check_fails_closed_and_never_treats_redirect_as_connected(self):
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw) / "secrets" / "ifixit_pro_firefox"
            profile.mkdir(parents=True)
            c = sqlite3.connect(profile / "cookies.sqlite")
            c.execute(
                "CREATE TABLE moz_cookies(host TEXT,path TEXT,isSecure INTEGER,"
                "expiry INTEGER,name TEXT,value TEXT)"
            )
            c.execute(
                "INSERT INTO moz_cookies VALUES('.ifixit.com','/',1,4102444800,'session','x')"
            )
            c.commit(); c.close()
            with patch(
                "mikrofix_ifixit._authenticated_open",
                return_value=("https://www.ifixit.com/Login", ""),
            ):
                self.assertFalse(pro_session_connected(profile))


if __name__ == "__main__":
    unittest.main()
