import sqlite3
import tempfile
import unittest
from pathlib import Path

from mikrofix_fixshop import (
    _cookie_jar,
    disconnect_pro_session,
    match_modules,
    parse_offers,
    parts_url,
    pro_profile_path,
)


class FixShopPriceTests(unittest.TestCase):
    def test_model_url_uses_exact_fixshop_category(self):
        self.assertEqual(
            parts_url("Apple iPhone 11 Pro"),
            "https://fixshop.si/rezervni-deli-apple-iphone-apple-iphone-11-pro/",
        )

    def test_parser_matches_real_card_shape_and_rejects_accessories(self):
        def card(url, title, price):
            return f'''<div class="product-card-wrapper product-card-wrapper-grid">
            <div class="product-card"><a href="{url}" title="{title}"
            class="product-card-image flex-shrink-0"></a>
            <p class="font-bold text-p-small"> {price} € </p></div></div>'''

        document = "".join((
            card("/screen/", "Zaslon OLED za iPhone 11 Pro", "74,37"),
            card("/adhesive/", "Lepilo za zaslon iPhone 11 Pro", "2,46"),
            card("/charge/", "iPhone 11 Pro - Konektor za polnjenje + Flex kabel", "26,76"),
            card("/camera-glass/", "iPhone 11 Pro - Steklo zadnje kamere", "2,46"),
            card("/camera/", "iPhone 11 Pro - zadnja kamera", "44,61"),
            card("/speaker/", "iPhone 11 Pro - zvočnik", "3,95"),
        ))
        matched = match_modules(parse_offers(document))
        self.assertEqual(matched["DISPLAY"].price_eur, 74.37)
        self.assertEqual(matched["CHARGING_PORT"].price_eur, 26.76)
        self.assertEqual(matched["REAR_CAMERA"].price_eur, 44.61)
        self.assertEqual(matched["LOUDSPEAKER"].price_eur, 3.95)

    def test_profile_is_isolated_and_only_fixshop_cookies_are_read(self):
        self.assertEqual(
            pro_profile_path("/data/sandbox/db/mikrofix_inventory.db"),
            Path("/data/sandbox/secrets/fixshop_pro_firefox"),
        )
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw) / "secrets" / "fixshop_pro_firefox"
            profile.mkdir(parents=True)
            connection = sqlite3.connect(profile / "cookies.sqlite")
            connection.execute(
                "CREATE TABLE moz_cookies(host TEXT,path TEXT,isSecure INTEGER,"
                "expiry INTEGER,name TEXT,value TEXT)"
            )
            connection.execute(
                "INSERT INTO moz_cookies VALUES('.fixshop.si','/',1,4102444800,'session','secret')"
            )
            connection.execute(
                "INSERT INTO moz_cookies VALUES('.example.com','/',1,4102444800,'other','ignore')"
            )
            connection.commit(); connection.close()
            self.assertEqual(
                [(cookie.domain, cookie.name) for cookie in _cookie_jar(profile)],
                [(".fixshop.si", "session")],
            )
            disconnect_pro_session(profile)
            self.assertFalse((profile / "cookies.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
