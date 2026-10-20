"""Jednorazowe logowanie do Cookidoo i zapis tokenu.

Po udanym logowaniu serwer MCP korzysta z zapisanego tokenu i odswieza go sam,
wiec hasla nie trzeba trzymac w konfiguracji Claude'a.

    python login.py

Gdy logowanie haslem odbije sie od zabezpieczen Cloudflare, skrypt przechodzi
na tryb przegladarkowy: wypisuje link, ktory otwierasz u siebie, logujesz sie,
a potem wklejasz adres, na ktory przegladarka probowala Cie przekierowac
(zaczyna sie od com.vorwerk.cookidoo://).
"""

from __future__ import annotations

import asyncio
import os
import sys

from aiohttp import ClientSession, CookieJar
from dotenv import load_dotenv

from cookidoo_api import Cookidoo, CookidooConfig, CookidooException

from server import LOCALIZATION, STATE_DIR, TOKEN_FILE


async def _run() -> int:
    load_dotenv()
    email = os.getenv("COOKIDOO_EMAIL", "")
    password = os.getenv("COOKIDOO_PASSWORD", "")

    if not email:
        email = input("E-mail Cookidoo: ").strip()
    if not password:
        import getpass

        password = getpass.getpass("Haslo Cookidoo: ")

    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
        client = Cookidoo(
            session,
            CookidooConfig(
                localization=LOCALIZATION, email=email, password=password
            ),
        )

        try:
            await client.login()
        except CookidooException as err:
            print(f"Logowanie haslem nie wyszlo: {err}\n", file=sys.stderr)
            print("Przechodze na tryb przegladarkowy.\n")
            url = await client.begin_login()
            print("1. Otworz ten adres w przegladarce i zaloguj sie:\n")
            print(f"   {url}\n")
            print(
                "2. Po zalogowaniu przegladarka probuje przejsc na adres\n"
                "   com.vorwerk.cookidoo://code-grant?...  — skopiuj go\n"
                "   (moze byc w pasku adresu lub w komunikacie o bledzie).\n"
            )
            response_url = input("3. Wklej tutaj ten adres: ").strip()
            await client.complete_login(response_url)

        user = await client.get_user_info()
        subscription = await client.get_active_subscription()
        devices = await client.get_devices()

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        client.save_token(TOKEN_FILE)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass

        print("\nZalogowano.")
        print(f"  konto:       {getattr(user, 'name', None) or email}")
        print(f"  subskrypcja: {subscription or 'brak aktywnej'}")
        print(
            "  urzadzenia:  "
            + (", ".join(d.type.value for d in devices) or "brak sparowanych")
        )
        print(f"  token:       {TOKEN_FILE}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except CookidooException as error:
        print(f"Blad: {error}", file=sys.stderr)
        raise SystemExit(1) from None
