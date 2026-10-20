"""MCP server dla Thermomixa TM6 — przez chmure Cookidoo.

Czego TU NIE MA, bo nie istnieje: TM6 nie ma lokalnego API ani zdalnego
sterowania. Nie da sie z zewnatrz ustawic temperatury, obrotow czy wystartowac
gotowania. Vorwerk nie udostepnia zadnego publicznego API.

Co da sie zrobic i co ten serwer robi, na koncie Cookidoo:
  * szukanie przepisow przefiltrowanych pod TM6 i odczyt ich szczegolow,
  * tworzenie / edycja / usuwanie WLASNYCH przepisow z adnotacjami guided
    cooking (czas, temperatura, obroty, tryby) — taki przepis pojawia sie
    na ekranie TM6 i urzadzenie prowadzi przez kroki,
  * lista zakupow (skladniki z przepisow, wlasne pozycje, odhaczanie),
  * kalendarz "moj tydzien",
  * kolekcje,
  * podglad trwajacego gotowania (remote monitoring) — TYLKO ODCZYT.

Oparte na nieoficjalnej bibliotece cookidoo-api. Projekt nie jest w zaden
sposob powiazany z Vorwerk ani Cookidoo i moze przestac dzialac po zmianach
po stronie Cookidoo.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timedelta
from enum import Enum
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, CookieJar, FormData
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cookidoo_api import (
    Cookidoo,
    CookidooAuthData,
    CookidooConfig,
    CookidooCreateCustomRecipe,
    CookidooCustomAnnotation,
    CookidooException,
    CookidooInstruction,
    CookidooLocalizationConfig,
    CookidooModeAnnotation,
    CookidooRemoteMonitoring,
    CookidooTemperatureSetting,
    CookidooTTSAnnotation,
    CookidooUpdateCustomRecipe,
    ThermomixMachineType,
)
from cookidoo_api.types import CookidooCookingActivity

load_dotenv()

_LOGGER = logging.getLogger("thermomix-mcp")

STATE_DIR = Path(os.getenv("THERMOMIX_MCP_STATE") or (Path.home() / ".thermomix-mcp"))
TOKEN_FILE = STATE_DIR / "token.json"
FCM_FILE = STATE_DIR / "fcm.json"
APP_ID_FILE = STATE_DIR / "app-id"

LOCALIZATION = CookidooLocalizationConfig(
    country_code=os.getenv("COOKIDOO_COUNTRY", "pl"),
    language=os.getenv("COOKIDOO_LANGUAGE", "pl-PL"),
    url=os.getenv("COOKIDOO_URL", "https://cookidoo.pl/foundation/pl-PL"),
)

MODEL = ThermomixMachineType(os.getenv("THERMOMIX_MODEL", "TM6"))

READ_ONLY = ToolAnnotations(read_only_hint=True)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True, idempotent_hint=False)


@asynccontextmanager
async def _lifespan(_server: MCPServer) -> AsyncIterator[None]:
    """Zamknij sesje HTTP i monitoring przy wylaczaniu serwera."""
    try:
        yield
    finally:
        await CLIENT.close()


mcp = MCPServer(
    "thermomix",
    version="1.0.0",
    lifespan=_lifespan,
    instructions=(
        f"Dostep do konta Cookidoo uzytkownika dla Thermomixa {MODEL.value}. "
        "Zdalne sterowanie urzadzeniem NIE jest mozliwe — mozna czytac i "
        "tworzyc przepisy (z guided cooking), prowadzic liste zakupow i "
        "kalendarz oraz podgladac trwajace gotowanie (tylko odczyt). Wlasny "
        "przepis z adnotacjami czasu/temperatury/obrotow trafia na ekran "
        "Thermomixa i urzadzenie prowadzi uzytkownika przez kroki."
    ),
)


# ---------------------------------------------------------------------------
# serializacja
# ---------------------------------------------------------------------------


def _plain(obj: Any) -> Any:
    """Zamien dataclassy, enumy i daty na struktury nadajace sie na JSON."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return _plain(asdict(obj))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_plain(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# polaczenie i logowanie
# ---------------------------------------------------------------------------


class _Client:
    """Jedna sesja aiohttp i jeden zalogowany klient Cookidoo na proces."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._session: ClientSession | None = None
        self._cookidoo: Cookidoo | None = None
        self._monitor: CookidooRemoteMonitoring | None = None
        self.activities: dict[str, CookidooCookingActivity] = {}

    def _persist_token(self, _auth: CookidooAuthData) -> None:
        if self._cookidoo is None:
            return
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            self._cookidoo.save_token(TOKEN_FILE)
            os.chmod(TOKEN_FILE, 0o600)
        except OSError as err:
            _LOGGER.warning("Nie udalo sie zapisac tokenu: %s", err)

    async def get(self) -> Cookidoo:
        """Zwroc zalogowanego klienta; loguj lub odswiez token gdy trzeba."""
        async with self._lock:
            if self._cookidoo is not None:
                return self._cookidoo

            email = os.getenv("COOKIDOO_EMAIL", "")
            password = os.getenv("COOKIDOO_PASSWORD", "")

            session = ClientSession(cookie_jar=CookieJar(unsafe=True))
            client = Cookidoo(
                session,
                CookidooConfig(
                    localization=LOCALIZATION, email=email, password=password
                ),
            )
            # Referencje ustawiamy przed callbackiem — save_token ich uzywa.
            self._session = session
            self._cookidoo = client
            client.on_auth_data_update = self._persist_token

            restored = False
            if TOKEN_FILE.exists():
                try:
                    client.load_token(TOKEN_FILE)
                    await client.refresh()
                    restored = True
                except (CookidooException, OSError, ValueError) as err:
                    _LOGGER.info("Zapisany token nie dziala (%s), loguje od nowa.", err)

            if not restored:
                if not email or not password:
                    await self._reset(session)
                    raise ToolError(
                        "Nie ma dzialajacego tokenu ani danych logowania do "
                        "Cookidoo. Odpal `python login.py` w katalogu serwera "
                        "albo ustaw COOKIDOO_EMAIL i COOKIDOO_PASSWORD w .env."
                    )
                try:
                    await client.login()
                except CookidooException as err:
                    await self._reset(session)
                    raise ToolError(
                        f"Logowanie do Cookidoo nie udalo sie: {err}. "
                        "Sprawdz e-mail i haslo, a jesli sa dobre — odpal "
                        "`python login.py` (ma tryb przegladarkowy, ktory "
                        "obchodzi zabezpieczenia logowania haslem)."
                    ) from err
                if client.auth_data is not None:
                    self._persist_token(client.auth_data)

            return client

    async def _reset(self, session: ClientSession) -> None:
        """Porzuc nieudane polaczenie, zeby kolejne wywolanie probowalo od nowa."""
        self._cookidoo = None
        self._session = None
        await session.close()

    async def close(self) -> None:
        """Zatrzymaj monitoring i zamknij sesje HTTP (wolane przy wylaczaniu)."""
        if self._monitor is not None:
            try:
                await self._monitor.stop()
            except Exception as err:  # noqa: BLE001 - zamykanie nie moze wysypac
                _LOGGER.debug("Blad przy zatrzymywaniu monitoringu: %s", err)
            self._monitor = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._cookidoo = None

    # ---- remote monitoring ------------------------------------------------

    def _app_id(self) -> str:
        """Stabilny identyfikator tej instalacji (dla rejestracji pushy)."""
        if APP_ID_FILE.exists():
            existing = APP_ID_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        app_id = str(uuid.uuid4())
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        APP_ID_FILE.write_text(app_id, encoding="utf-8")
        return app_id

    def _persist_fcm(self, credentials: dict[str, Any]) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            FCM_FILE.write_text(json.dumps(credentials), encoding="utf-8")
            os.chmod(FCM_FILE, 0o600)
        except OSError as err:
            _LOGGER.warning("Nie udalo sie zapisac danych FCM: %s", err)

    def _on_activity(self, activity: CookidooCookingActivity) -> None:
        self.activities[activity.device_id] = activity

    async def monitor(self) -> CookidooRemoteMonitoring:
        """Wystartuj (albo zwroc dzialajacy) odbiornik stanu gotowania."""
        if self._monitor is not None and self._monitor.is_connected:
            return self._monitor

        client = await self.get()
        credentials: dict[str, Any] | None = None
        if FCM_FILE.exists():
            try:
                credentials = json.loads(FCM_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                credentials = None

        monitor = CookidooRemoteMonitoring(
            client,
            self._on_activity,
            mobile_app_id=self._app_id(),
            credentials=credentials,
            on_credentials=self._persist_fcm,
        )
        await monitor.start()
        self._monitor = monitor
        return monitor


CLIENT = _Client()


# ---------------------------------------------------------------------------
# budowanie krokow guided cooking
# ---------------------------------------------------------------------------


def _temperature(value: Any) -> CookidooTemperatureSetting:
    """Przyjmij 100, "100" albo "varoma".

    Cookidoo trzyma stopnie jako napis ("120", nie 120) — trzymamy sie tego,
    zeby wgrane przepisy mialy strukture identyczna z przepisami platformy.
    """
    if isinstance(value, str) and value.strip().lower() == "varoma":
        return CookidooTemperatureSetting(value="varoma", unit=None)
    return CookidooTemperatureSetting(value=str(int(value)), unit="C")


# Ikona lewych obrotow z fontu Cookidoo. W przepisach platformy stoi jako
# osobny segment frazy, miedzy temperatura a obrotami: "10 min/100°C/<ikona>/obr. 1".
LEFT_ROTATION = ""

# Ikona obrotow delikatnych ("soft"). Platforma zapisuje je jako "obr. <ikona>",
# nigdy slowem — potwierdzone w 23 przepisach Vorwerka.
SOFT_SPEED = ""

# Tryby prowadzone: klucz biblioteki -> (nazwa po polsku, ikona fontu Cookidoo).
# Fraza w tekscie kroku ma postac "<Nazwa> <ikona>" i dalej zwykle segmenty
# (czas/temperatura/lewe obroty/obroty), np. "Ciasto /2 min",
# "Podgrzewanie /90°C/obr. 2", "Wysokie Temp. ".
#
# Zrodlem sa przepisy platformy, nie domysly (skan 120 przepisow z konta):
#   dough    — potwierdzony do konca: adnotacja mode="dough" ze slotem
#              "Ciasto /2 min" lezy na koncie w 3 przepisach.
#   blend, warm_up — ikona i nazwa odczytane z przepisow platformy.
#   browning — TM6 nazywa ten tryb "Wysokie Temp."; ikona odczytana, ale
#              powiazania z kluczem "browning" nie da sie potwierdzic bez
#              przepisu, ktory ma te adnotacje zapisana strukturalnie.
# Trybow turbo, steaming i rice_cooker nie znaleziono w zadnym przepisie —
# nie wymyslamy dla nich ikon. Dostana sama nazwe (patrz _mode_prefix).
MODES: dict[str, tuple[str, str]] = {
    "dough": ("Ciasto", ""),
    "blend": ("Miksowanie", ""),
    "warm_up": ("Podgrzewanie", ""),
    "browning": ("Wysokie Temp.", ""),
}

# Nazwy dla trybow bez potwierdzonej ikony — lepiej pokazac uzytkownikowi
# nazwe trybu niz samy czas, ale bez zmyslonego znaku z fontu.
MODE_NAMES: dict[str, str] = {
    "turbo": "Turbo",
    "steaming": "Gotowanie na parze",
    "rice_cooker": "Ryz",
}


def _mode_prefix(mode: str) -> str:
    """Poczatek frazy dla trybu: "Ciasto " albo sama nazwa, gdy ikona
    nieznana. Nieznany klucz zwraca sam tekst klucza — walidacje robi biblioteka.
    """
    key = str(mode).strip().lower()
    if key in MODES:
        name, glyph = MODES[key]
        return f"{name} {glyph}"
    return MODE_NAMES.get(key, str(mode))


# Ulamki, ktorych Cookidoo uzywa w gramaturach.
FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125}

# Slowa traktowane jako jednostka miary. Rzeczowniki policzalne (zabek, ziarna,
# liscie) NIE sa jednostkami — przy nich Cookidoo obejmuje adnotacja sama liczbe
# i nie zapisuje pola "unit".
UNITS = {
    "g", "kg", "mg", "dag", "ml", "l", "dl",
    "łyżka", "łyżki", "łyżek", "łyżeczka", "łyżeczki", "łyżeczek",
    "szczypta", "szczypty", "szczypt", "szklanka", "szklanki", "szklanek",
    "opakowanie", "opakowania", "puszka", "puszki", "kostka", "kostki",
}

_QUANTITY_RE = re.compile(
    r"^(?P<a>\d+(?:[.,]\d+)?|[½¼¾⅓⅔⅛])"
    r"(?:\s*-\s*(?P<b>\d+(?:[.,]\d+)?|[½¼¾⅓⅔⅛]))?"
    r"(?:\s+(?P<unit>\S+))?"
)


def _number(token: str) -> float | int:
    """Zamien "150", "1,5" albo "½" na liczbe."""
    if token in FRACTIONS:
        return FRACTIONS[token]
    value = float(token.replace(",", "."))
    return int(value) if value.is_integer() else value


def _volume_annotation(description: str) -> dict[str, Any] | None:
    """Zbuduj adnotacje VOLUME dla gramatury na poczatku opisu skladnika.

    To ona sprawia, ze Thermomix pokazuje ekran wagi i wie, ile odwazyc.
    Bez niej skladnik jest tylko podswietlony w tekscie kroku.
    """
    match = _QUANTITY_RE.match(description.strip())
    if match is None:
        return None

    data: dict[str, Any] = {"amount": _number(match.group("a"))}
    if match.group("b"):
        data["amountMax"] = _number(match.group("b"))

    span = match.group(0)
    unit = match.group("unit")
    if unit is not None:
        if unit.strip(".,;:").lower() in UNITS:
            data["unit"] = unit.strip(".,;:")
        else:
            # Nastepne slowo to rzeczownik, nie jednostka: adnotacja obejmuje
            # sama liczbe (albo sam zakres), bez tego slowa.
            span = span[: len(span) - len(unit)].rstrip()

    return {
        "type": "VOLUME",
        "position": {
            "offset": 0,
            "length": len(span.encode("utf-16-le")) // 2,
        },
        "data": data,
    }


def _format_time(seconds: int) -> str:
    """Zapis czasu w konwencji Cookidoo: "3 s", "5 min", "1 godz 30 min"."""
    if seconds < 60:
        return f"{seconds} s"
    if seconds % 60 == 0 and seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours} godz." if minutes == 0 else f"{hours} godz. {minutes} min"


def _settings_phrase(
    time_s: Any, temperature: Any, speed: Any, direction: Any, mode: Any = None
) -> str:
    """Zbuduj fraze ustawien dokladnie tak, jak zapisuje ja platforma.

    Przyklady z przepisow Cookidoo:
      "5 s/obr. 5", "3 min/120°C/obr. 1", "20 min/100°C/<ikona lewych>/obr. 1",
      "5 min/120°C/<ikona lewych>/obr. <ikona soft>", "Ciasto <ikona>/2 min",
      "Podgrzewanie <ikona>/90°C/obr. 2", "Wysokie Temp. <ikona>".

    Tryb (gdy podany) daje przedrostek "<Nazwa> <ikona>", a dalej ida te same
    segmenty co zwykle. Ta jedna regula odtwarza wszystkie zaobserwowane
    warianty, w tym tryby bez czasu i bez obrotow.
    """
    parts: list[str] = []
    if mode is not None:
        parts.append(_mode_prefix(mode))
    if time_s is not None:
        parts.append(_format_time(int(time_s)))
    if temperature is not None:
        if str(temperature).strip().lower() == "varoma":
            parts.append("Varoma")
        else:
            parts.append(f"{int(temperature)}°C")
    if direction is not None and str(direction).upper() == "CCW":
        parts.append(LEFT_ROTATION)
    if speed is not None:
        # Obroty delikatne platforma zapisuje ikona, nie slowem "soft".
        parts.append(
            f"obr. {SOFT_SPEED}"
            if str(speed).lower() == "soft"
            else f"obr. {speed}"
        )
    return "/".join(parts)


def _instruction(
    step: dict[str, Any], index: int, known: set[str] | None = None
) -> str | CookidooInstruction:
    """Zamien slownik kroku na instrukcje z adnotacja guided cooking.

    Guided cooking na TM6 dziala tylko dla ustawien "podswietlonych" w tresci
    kroku (adnotacja przypieta do fragmentu tekstu). Dlatego gdy wywolujacy nie
    poda 'slot', fraza ustawien jest budowana i dopisywana do tekstu kroku, a
    adnotacja przypinana do niej. Krok bez ustawien zostaje czystym tekstem.
    """
    text = str(step.get("text") or "").strip()
    if not text:
        raise ValueError(f"Krok {index + 1}: brak pola 'text'.")

    time_s = step.get("time")
    temperature = step.get("temperature")
    speed = step.get("speed")
    direction = step.get("direction")
    mode = step.get("mode")
    slot = step.get("slot")

    # Powiazania skladnikow: {fragment tekstu kroku: doslowna pozycja z listy}.
    # Tak samo robi to edytor Cookidoo ("polacz skladniki z krokami") i dzieki
    # temu skladnik jest podswietlony podczas prowadzenia na TM.
    links: dict[str, str] = dict(step.get("ingredients") or {})
    ingredient_annotations: list[CookidooCustomAnnotation] = []
    for fragment, exact in links.items():
        if fragment not in text:
            raise ValueError(
                f"Krok {index + 1}: fragment {fragment!r} z powiazan skladnikow "
                f"nie wystepuje w tresci kroku ({text!r})."
            )
        exact = str(exact)
        if known is not None and exact not in known:
            raise ValueError(
                f"Krok {index + 1}: skladnik {exact!r} nie wystepuje na liscie "
                f"skladnikow przepisu. Musi byc przepisany znak w znak."
            )
        # Opis to obiekt z zagniezdzona adnotacja VOLUME — dopiero ona mowi
        # Thermomixowi, ile odwazyc, i uruchamia ekran wagi.
        description: dict[str, Any] = {"text": exact}
        volume = _volume_annotation(exact)
        if volume is not None:
            description["annotations"] = [volume]
        ingredient_annotations.append(
            CookidooCustomAnnotation(
                type="INGREDIENT", slot=fragment, data={"description": description}
            )
        )

    if all(v is None for v in (time_s, temperature, speed, direction, mode)):
        if ingredient_annotations:
            return CookidooInstruction(text=text, annotations=list(ingredient_annotations))
        return text

    if slot:
        if str(slot) not in text:
            raise ValueError(
                f"Krok {index + 1}: 'slot' ({slot!r}) musi byc doslownym "
                f"fragmentem 'text' ({text!r})."
            )
        slot = str(slot)
    else:
        # Brak slotu — dopisz fraze ustawien do tekstu, zeby TM6 mial co
        # podswietlic i poprowadzic. Bez tego krok jest tylko do przeczytania.
        phrase = _settings_phrase(time_s, temperature, speed, direction, mode)
        if not phrase:
            return text
        if phrase in text:
            slot = phrase
        else:
            # Konwencja Cookidoo: fraza wpleciona wprost w zdanie, bez myslnika,
            # i kropka na koncu ("Dodac olive, dusic 3 min/120°C/obr. 1.").
            text = f"{text.rstrip('.').rstrip()} {phrase}."
            slot = phrase

    temp = _temperature(temperature) if temperature is not None else None
    annotation: CookidooTTSAnnotation | CookidooModeAnnotation
    if mode is not None:
        annotation = CookidooModeAnnotation(
            slot=slot,
            mode=str(mode),
            time=int(time_s) if time_s is not None else None,
            temperature=temp,
            speed=str(speed) if speed is not None else None,
            direction=str(direction) if direction is not None else None,
            power=str(step["power"]) if step.get("power") is not None else None,
            accessory=(
                str(step["accessory"]) if step.get("accessory") is not None else None
            ),
        )
    else:
        annotation = CookidooTTSAnnotation(
            slot=slot,
            time=int(time_s) if time_s is not None else None,
            temperature=temp,
            speed=str(speed) if speed is not None else None,
            direction=str(direction) if direction is not None else None,
        )
    # Kolejnosc jak w przepisach Cookidoo: najpierw skladniki, potem ustawienia.
    return CookidooInstruction(
        text=text, annotations=[*ingredient_annotations, annotation]
    )


def _instructions(
    steps: list[dict[str, Any]], ingredients: list[str] | None = None
) -> list[str | CookidooInstruction]:
    """Zamien kroki na instrukcje; ingredients sluzy do walidacji powiazan."""
    known = set(ingredients) if ingredients is not None else None
    return [_instruction(step, i, known) for i, step in enumerate(steps)]


def _readable(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Przepusc powod bledu do modelu zamiast maskowac go jako awarie.

    MCPServer pokazuje tresc tylko dla ToolError — pozostale wyjatki laduja
    jako "Error executing tool", co nic nie mowi ani modelowi, ani uzytkownikowi.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except ValueError as err:
            # Walidacja krokow i przepisu — te komunikaty sa konkretne.
            raise ToolError(str(err)) from err
        except CookidooException as err:
            raise ToolError(f"Cookidoo zwrocil blad: {err}") from err

    return wrapper


# ---------------------------------------------------------------------------
# konto i urzadzenie
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@_readable
async def account_status() -> dict[str, Any]:
    """Stan konta Cookidoo: uzytkownik, subskrypcja i sparowane Thermomixy.

    Dobre na start — potwierdza, ze logowanie dziala i pokazuje, czy na koncie
    jest TM6 oraz czy subskrypcja jest aktywna (bez niej czesc przepisow
    Cookidoo jest niedostepna).
    """
    client = await CLIENT.get()
    user = await client.get_user_info()
    subscription = await client.get_active_subscription()
    devices = await client.get_devices()
    monitored = await client.get_monitored_device_ids()
    return {
        "uzytkownik": _plain(user),
        "subskrypcja": _plain(subscription),
        "urzadzenia": _plain(devices),
        "urzadzenia_z_monitoringiem": monitored,
        "lokalizacja": _plain(LOCALIZATION),
        "model_domyslny": MODEL.value,
        "sterowanie_zdalne": (
            "niedostepne — TM6 nie ma API do sterowania, mozliwy tylko podglad"
        ),
    }


@mcp.tool(annotations=READ_ONLY)
@_readable
async def cooking_status() -> dict[str, Any]:
    """Co Thermomix robi teraz: krok, czas do konca, temperatura. Tylko odczyt.

    UWAGA: remote monitoring Cookidoo to funkcja TM7. Na TM6 ten podglad nie
    dziala — tam odpowiednikiem jest czujnik Thermomix Sensor z aplikacja
    Cooking Center, poza API Cookidoo. Przy TM6 narzedzie zwroci wyjasnienie,
    a nie stan gotowania.

    Stan przychodzi pushem, wiec na TM7 pierwsze wywolanie moze zwrocic jeszcze
    pustke — wtedy zapytaj ponownie po kilku sekundach.
    """
    client = await CLIENT.get()
    monitored = await client.get_monitored_device_ids()
    if not monitored:
        devices = [d.type.value for d in await client.get_devices()]
        return {
            "dostepny": False,
            "urzadzenia_na_koncie": devices,
            "powod": (
                "Zaden Thermomix nie jest zgloszony do remote monitoringu. "
                "Na TM6 to normalne: podglad gotowania przez Cookidoo to "
                "funkcja TM7. Dla TM6 sluzy do tego czujnik Thermomix Sensor "
                "i aplikacja Cooking Center, do ktorych to API nie siega."
                if "TM7" not in devices
                else "Urzadzenie jest offline albo nie gotuje w tej chwili."
            ),
        }

    try:
        monitor = await CLIENT.monitor()
    except (CookidooException, OSError) as err:
        return {
            "blad": f"Nie udalo sie uruchomic monitoringu: {err}",
            "podpowiedz": (
                "Monitoring wymaga aktywnej subskrypcji i urzadzenia online. "
                "Sprawdz account_status."
            ),
        }

    activities = [_plain(a) for a in CLIENT.activities.values()]
    active = [
        a for a in CLIENT.activities.values() if a.is_active
    ]
    return {
        "podlaczony": monitor.is_connected,
        "gotowanie_w_toku": bool(active),
        "aktywnosci": activities,
        "uwaga": (
            "Brak aktywnosci nie znaczy, ze Thermomix stoi — stan pojawia sie "
            "tylko gdy urzadzenie gotuje przepis przy wlaczonym podgladzie."
        )
        if not activities
        else None,
    }


# ---------------------------------------------------------------------------
# przepisy Cookidoo
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@_readable
async def search_recipes(
    query: str | None = None,
    ingredients: str | None = None,
    exclude_ingredients: str | None = None,
    max_total_time_min: int | None = None,
    difficulty: str | None = None,
    portions: int | None = None,
    page: int | None = None,
    page_size: int | None = 20,
    all_models: bool = False,
) -> dict[str, Any]:
    """Szukaj przepisow w Cookidoo, domyslnie tylko tych pod TM6.

    Args:
        query: Fraza, np. "zupa krem z dyni".
        ingredients: Skladniki po przecinku, ktore maja wystapic.
        exclude_ingredients: Skladniki po przecinku do wykluczenia.
        max_total_time_min: Maksymalny calkowity czas w minutach.
        difficulty: "easy", "medium" albo "hard".
        portions: Liczba porcji.
        page: Numer strony wynikow.
        page_size: Ile wynikow na strone (domyslnie 20).
        all_models: True = nie filtruj po modelu urzadzenia.
    """
    client = await CLIENT.get()
    result = await client.search_recipes(
        query,
        ingredients=ingredients,
        exclude_ingredients=exclude_ingredients,
        total_time=max_total_time_min * 60 if max_total_time_min else None,
        difficulty=difficulty,
        portions=portions,
        page=page,
        page_size=page_size,
        tmv=None if all_models else MODEL,
    )
    return {
        "razem": result.total,
        "model": "wszystkie" if all_models else MODEL.value,
        "przepisy": _plain(result.recipes),
    }


@mcp.tool(annotations=READ_ONLY)
@_readable
async def recipe_details(recipe_id: str) -> dict[str, Any]:
    """Pelne szczegoly przepisu Cookidoo: skladniki, kroki, czasy, wartosci.

    Args:
        recipe_id: Id przepisu, np. "r59322" (z wynikow search_recipes).
    """
    client = await CLIENT.get()
    return _plain(await client.get_recipe_details(recipe_id))


# ---------------------------------------------------------------------------
# wlasne przepisy — tu powstaje guided cooking dla TM6
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@_readable
async def my_recipes() -> dict[str, Any]:
    """Lista wlasnych przepisow na koncie (tych tworzonych przez create_recipe)."""
    client = await CLIENT.get()
    recipes = await client.list_custom_recipes()
    return {"liczba": len(recipes), "przepisy": _plain(recipes)}


@mcp.tool(annotations=READ_ONLY)
@_readable
async def preview_recipe(
    name: str,
    ingredients: list[str],
    steps: list[dict[str, Any]],
    serving_size: int = 4,
    total_time_min: int = 30,
    active_time_min: int = 15,
) -> dict[str, Any]:
    """Sprawdz, jak przepis wyjdzie na TM6 — BEZ wgrywania na konto.

    Zwraca kroki w finalnej formie (z dopisanymi frazami ustawien) i mowi,
    ktore kroki beda prowadzone przez urzadzenie, a ktore zostana samym
    tekstem. Uzyj tego, zeby pokazac uzytkownikowi wynik konwersji do
    zatwierdzenia, a dopiero potem wywolaj create_recipe.

    Args: takie same jak w create_recipe (bez hints).
    """
    instructions = _instructions(steps, list(ingredients))
    rendered: list[dict[str, Any]] = []
    guided = 0
    for i, instruction in enumerate(instructions, start=1):
        if isinstance(instruction, str):
            rendered.append(
                {
                    "nr": i,
                    "tekst": instruction,
                    "prowadzony": False,
                    "podswietlone": None,
                    "ustawienia": None,
                    "skladniki_powiazane": [],
                }
            )
            continue
        linked = [
            a.slot
            for a in instruction.annotations
            if getattr(a, "type", None) == "INGREDIENT"
        ]
        settings = next(
            (
                a
                for a in instruction.annotations
                if getattr(a, "type", None) != "INGREDIENT"
            ),
            None,
        )
        if settings is not None:
            guided += 1
        rendered.append(
            {
                "nr": i,
                "tekst": instruction.text,
                "prowadzony": settings is not None,
                "podswietlone": getattr(settings, "slot", None),
                "ustawienia": _plain(settings),
                "skladniki_powiazane": linked,
            }
        )
    return {
        "nazwa": name,
        "model": MODEL.value,
        "porcje": serving_size,
        "czas_calkowity_min": total_time_min,
        "czas_pracy_min": active_time_min,
        "skladniki": list(ingredients),
        "kroki": rendered,
        "krokow_prowadzonych": guided,
        "krokow_tekstowych": len(instructions) - guided,
        "wgrane": False,
    }


@mcp.tool()
@_readable
async def create_recipe(
    name: str,
    ingredients: list[str],
    steps: list[dict[str, Any]],
    serving_size: int = 4,
    total_time_min: int = 30,
    active_time_min: int = 15,
    hints: list[str] | None = None,
) -> dict[str, Any]:
    """Zapisz wlasny przepis na koncie — z guided cooking dla Thermomixa.

    Przepis pojawi sie na ekranie TM6 (Moje przepisy) i urzadzenie poprowadzi
    przez kroki. Krok z ustawieniami to najblizsza dostepna forma "sterowania":
    nie wystartujesz gotowania zdalnie, ale uzytkownik tylko potwierdza krok,
    a czas, temperatura i obroty sa juz ustawione.

    Args:
        name: Nazwa przepisu.
        ingredients: Skladniki, po jednym na pozycje, np. "200 g maki".
        steps: Kroki. Kazdy to slownik z polem "text", opcjonalnie:
            - "time": czas w SEKUNDACH (np. 300 dla 5 min)
            - "temperature": liczba stopni C (np. 100) albo "varoma"
            - "speed": obroty jako tekst, np. "2", "5", "soft"
            - "direction": "CW" (w prawo) albo "CCW" (lewe obroty)
            - "slot": doslowny fragment "text", do ktorego przypiac ustawienia,
              np. "5 min/100°C/obr. 2". Pominiecie jest OK — fraza zostanie
              zbudowana i dopisana do tekstu kroku automatycznie
            - "mode": tryb ("dough", "browning", "turbo", "steaming", "blend",
              "warm_up", "rice_cooker"). Nazwa trybu i jego ikona z fontu
              Cookidoo sa dopisywane do frazy automatycznie, tak jak w
              przepisach platformy ("Ciasto <ikona>/2 min"). Ikony sa
              potwierdzone dla dough, blend, warm_up i browning; turbo,
              steaming i rice_cooker dostana sama nazwe, bo nie znaleziono
              ich w zadnym przepisie platformy
            - "power": dla trybu browning: "Gentle" albo "Intense"
            - "accessory": dla steaming: "Varoma", "SimmeringBasket",
              "VaromaAndSimmeringBasket"
            - "ingredients": powiazania skladnikow uzytych w tym kroku, jako
              {fragment tekstu kroku: doslowna pozycja z listy ingredients},
              np. {"oliwe": "20 g oliwy z oliwek"}. Dzieki temu skladnik jest
              podswietlony na ekranie TM w trakcie prowadzenia — tak samo jak
              w przepisach Cookidoo. Wypelniaj to dla kazdego skladnika
              wspomnianego w kroku.
        serving_size: Liczba porcji.
        total_time_min: Calkowity czas w minutach.
        active_time_min: Czas pracy wlasnej w minutach.
        hints: Dodatkowe wskazowki.

    Przyklad kroku:
        {"text": "Wloz cebule i posiekaj", "time": 5, "speed": "5"}
        -> na TM6: "Wloz cebule i posiekaj 5 s/obr. 5." z prowadzeniem
    """
    client = await CLIENT.get()
    recipe = CookidooCreateCustomRecipe(
        name=name,
        ingredients=list(ingredients),
        instructions=_instructions(steps, list(ingredients)),
        serving_size=serving_size,
        total_time=total_time_min * 60,
        active_time=active_time_min * 60,
        tools=[MODEL],
        hints=list(hints or []),
    )
    created = await client.create_custom_recipe(recipe)
    return {
        "utworzono": True,
        "model": MODEL.value,
        "przepis": _plain(created),
    }


@mcp.tool()
@_readable
async def update_recipe(
    recipe_id: str,
    name: str | None = None,
    ingredients: list[str] | None = None,
    steps: list[dict[str, Any]] | None = None,
    serving_size: int | None = None,
    total_time_min: int | None = None,
    active_time_min: int | None = None,
    hints: list[str] | None = None,
) -> dict[str, Any]:
    """Zmien wlasny przepis. Podaj tylko te pola, ktore maja sie zmienic.

    Args:
        recipe_id: Id wlasnego przepisu (z my_recipes).
        name: Nowa nazwa.
        ingredients: Nowa pelna lista skladnikow (zastepuje poprzednia).
        steps: Nowa pelna lista krokow — format jak w create_recipe.
        serving_size: Liczba porcji.
        total_time_min: Calkowity czas w minutach.
        active_time_min: Czas pracy wlasnej w minutach.
        hints: Wskazowki.
    """
    client = await CLIENT.get()
    patch = CookidooUpdateCustomRecipe(
        name=name,
        ingredients=list(ingredients) if ingredients is not None else None,
        instructions=(
            _instructions(steps, ingredients) if steps is not None else None
        ),
        serving_size=serving_size,
        total_time=total_time_min * 60 if total_time_min is not None else None,
        active_time=active_time_min * 60 if active_time_min is not None else None,
        hints=list(hints) if hints is not None else None,
    )
    return _plain(await client.update_custom_recipe(recipe_id, patch))


# Okladki. API Cookidoo nie przyjmuje adresu URL obrazu — obraz musi najpierw
# trafic do ich magazynu (Cloudinary), tak jak robi to edytor na stronie, a
# dopiero potem przepis dostaje jego sciezke. Ustalone doswiadczalnie:
#   1. POST <created-recipes>/image/signature z {timestamp, source:"uw", format}
#      -> {"signature": "<sha256>"}. Podpis zalezy WYLACZNIE od timestampu;
#      serwer podpisuje wlasny, staly zestaw parametrow.
#   2. POST do Cloudinary z polami: file, api_key, timestamp, signature,
#      signature_algorithm=sha256, source=uw, upload_preset. Dokladnie ten
#      zestaw — brak "source" albo "upload_preset" daje "Invalid Signature",
#      a dodanie "format" tez.
#   3. Odpowiedz niesie public_id (prod/img/customer-recipe/<nazwa>), ktory
#      idzie do update_custom_recipe(image=...).
# Klucz API, nazwa chmury i preset sa publiczne — strona edytora wkleja je w
# kod kazdemu uzytkownikowi — wiec czytamy je z tej strony zamiast na stale,
# zeby zmiana po stronie Vorwerka nie zepsula wgrywania po cichu.
IMAGE_EXTENSIONS = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png"}
IMAGE_MAX_BYTES = 10 * 1024 * 1024  # limit edytora Cookidoo (max-file-size)
IMAGE_SIGNING_URL = "https://cookidoo.pl/created-recipes/{language}/image/signature"
IMAGE_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


async def _image_widget_config(client: Any, recipe_id: str) -> dict[str, str]:
    """Odczytaj z edytora przepisu konfiguracje wgrywania obrazow."""
    url = (
        f"https://cookidoo.pl/created-recipes/{LOCALIZATION.language}"
        f"/{recipe_id}/edit"
    )
    async with CLIENT._session.get(
        url, headers={**client._api_headers, "Accept": "text/html"}
    ) as resp:
        if resp.status != 200:
            raise ValueError(
                f"Edytor przepisu {recipe_id} nie odpowiedzial (HTTP {resp.status}) "
                "— sprawdz id przepisu."
            )
        html = await resp.text()
    start = html.find('client="cloudinary"')
    if start < 0:
        raise ValueError(
            "Nie znaleziono konfiguracji wgrywania obrazow w edytorze Cookidoo "
            "— Vorwerk mogl zmienic strone."
        )
    tag = html[html.rfind("<", 0, start) : html.find(">", start) + 1]
    attrs = dict(IMAGE_ATTR_RE.findall(tag))
    missing = [k for k in ("api-key", "cloud-name", "upload-preset") if k not in attrs]
    if missing:
        raise ValueError(f"Edytor Cookidoo nie podal: {', '.join(missing)}.")
    return attrs


async def _upload_cover(client: Any, recipe_id: str, path: Path) -> str:
    """Wgraj plik do magazynu Cookidoo i zwroc sciezke obrazu dla przepisu."""
    cfg = await _image_widget_config(client, recipe_id)
    fmt = IMAGE_EXTENSIONS[path.suffix.lower()]
    sign_url = IMAGE_SIGNING_URL.format(language=LOCALIZATION.language)
    headers = {
        **client._api_headers,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    timestamp = int(time.time())
    signature = None
    for attempt in range(4):
        async with CLIENT._session.post(
            sign_url,
            headers=headers,
            data=json.dumps({"timestamp": timestamp, "source": "uw", "format": fmt}),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status == 200 and "signature" in body:
                signature = body["signature"]
                break
            if resp.status == 429:  # limit zapytan — "retry in N seconds"
                await asyncio.sleep(2 + attempt)
                timestamp = int(time.time())
                continue
            raise ValueError(f"Cookidoo odmowilo podpisu (HTTP {resp.status}): {body}")
    if signature is None:
        raise ValueError(
            "Cookidoo nie wydalo podpisu — za duzo zapytan, sprobuj za chwile."
        )

    form = FormData()
    form.add_field(
        "file",
        path.read_bytes(),
        filename=path.name,
        content_type="image/png" if fmt == "png" else "image/jpeg",
    )
    for key, value in (
        ("api_key", cfg["api-key"]),
        ("timestamp", str(timestamp)),
        ("signature", signature),
        ("signature_algorithm", "sha256"),
        ("source", "uw"),
        ("upload_preset", cfg["upload-preset"]),
    ):
        form.add_field(key, value)

    prefix = cfg.get("upload-prefix", "https://api-eu.cloudinary.com/").rstrip("/")
    upload_url = f"{prefix}/v1_1/{cfg['cloud-name']}/image/upload"
    # Osobna, nieuwierzytelniona sesja — Cloudinary nie ma dostawac tokenu Cookidoo.
    async with ClientSession() as plain, plain.post(upload_url, data=form) as resp:
        result = await resp.json(content_type=None)
        if resp.status != 200:
            detail = (result.get("error") or {}).get("message", result)
            raise ValueError(
                f"Wgranie obrazu nie powiodlo sie (HTTP {resp.status}): {detail}"
            )
    return f"{result['public_id']}.{result['format']}"


@mcp.tool()
@_readable
async def set_recipe_image(recipe_id: str, file_path: str) -> dict[str, Any]:
    """Ustaw okladke wlasnego przepisu z pliku na dysku (zamiast szarego kafelka).

    Wgrywa obraz do magazynu Cookidoo tak jak edytor na stronie i podpina go do
    przepisu. Kroki, skladniki i adnotacje zostaja nietkniete. Podmiana okladki
    nie kasuje poprzedniego pliku z magazynu.

    Args:
        recipe_id: Id wlasnego przepisu (z my_recipes).
        file_path: Sciezka do pliku JPG albo PNG, do 10 MB. Najlepiej poziomy,
            proporcje ok. 4:3 — kafelek Cookidoo ma 584x480 i przycina brzegi.
    """
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValueError(f"Nie ma pliku: {path}")
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(
            f"Obsluguje tylko JPG i PNG, a to {path.suffix or 'plik bez rozszerzenia'}."
        )
    size = path.stat().st_size
    if size > IMAGE_MAX_BYTES:
        raise ValueError(f"Plik ma {size / 1048576:.1f} MB, limit Cookidoo to 10 MB.")

    client = await CLIENT.get()
    image = await _upload_cover(client, recipe_id, path)
    updated = await client.update_custom_recipe(
        recipe_id, CookidooUpdateCustomRecipe(image=image)
    )
    return {
        "przepis": updated.name,
        "okladka": image,
        "adres": updated.image,
        "miniatura": updated.thumbnail,
        "url": updated.url,
    }


@mcp.tool(annotations=DESTRUCTIVE)
@_readable
async def delete_recipe(recipe_id: str) -> dict[str, Any]:
    """Usun wlasny przepis z konta. Nieodwracalne — upewnij sie co do id.

    Args:
        recipe_id: Id wlasnego przepisu (z my_recipes).
    """
    client = await CLIENT.get()
    await client.remove_custom_recipe(recipe_id)
    return {"usunieto": recipe_id}


# ---------------------------------------------------------------------------
# lista zakupow
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@_readable
async def shopping_list() -> dict[str, Any]:
    """Lista zakupow: skladniki z przepisow, wlasne pozycje i zrodlowe przepisy."""
    client = await CLIENT.get()
    ingredients = await client.get_ingredient_items()
    additional = await client.get_additional_items()
    recipes = await client.get_shopping_list_recipes()
    return {
        "skladniki": _plain(ingredients),
        "wlasne_pozycje": _plain(additional),
        "przepisy_na_liscie": _plain(recipes),
        "do_kupienia": sum(
            1 for i in [*ingredients, *additional] if not i.is_owned
        ),
    }


@mcp.tool()
@_readable
async def add_recipes_to_shopping_list(recipe_ids: list[str]) -> dict[str, Any]:
    """Dorzuc skladniki wskazanych przepisow Cookidoo na liste zakupow.

    Args:
        recipe_ids: Lista id przepisow, np. ["r59322"].
    """
    client = await CLIENT.get()
    items = await client.add_ingredient_items_for_recipes(list(recipe_ids))
    return {"dodano": len(items), "skladniki": _plain(items)}


@mcp.tool()
@_readable
async def add_shopping_items(items: list[str]) -> dict[str, Any]:
    """Dodaj wlasne pozycje na liste zakupow (rzeczy poza przepisami).

    Args:
        items: Nazwy pozycji, np. ["papier do pieczenia", "mleko 2%"].
    """
    client = await CLIENT.get()
    added = await client.add_additional_items(list(items))
    return {"dodano": len(added), "pozycje": _plain(added)}


@mcp.tool()
@_readable
async def check_off_shopping_items(
    item_ids: list[str], owned: bool = True
) -> dict[str, Any]:
    """Odhacz (albo odznacz) pozycje na liscie zakupow.

    Args:
        item_ids: Id pozycji — ze skladnikow lub wlasnych pozycji.
        owned: True = kupione/odhaczone, False = z powrotem do kupienia.
    """
    client = await CLIENT.get()
    wanted = set(item_ids)

    ingredients = [i for i in await client.get_ingredient_items() if i.id in wanted]
    additional = [i for i in await client.get_additional_items() if i.id in wanted]

    changed: list[Any] = []
    if ingredients:
        changed += await client.edit_ingredient_items_ownership(
            [replace(i, is_owned=owned) for i in ingredients]
        )
    if additional:
        changed += await client.edit_additional_items_ownership(
            [replace(i, is_owned=owned) for i in additional]
        )

    found = {i.id for i in [*ingredients, *additional]}
    return {
        "zmieniono": len(changed),
        "pozycje": _plain(changed),
        "nieznalezione": sorted(wanted - found),
    }


@mcp.tool(annotations=DESTRUCTIVE)
@_readable
async def remove_shopping_items(item_ids: list[str]) -> dict[str, Any]:
    """Usun wlasne pozycje z listy zakupow (te dodane przez add_shopping_items).

    Dotyczy tylko pozycji wlasnych — skladnikow pochodzacych z przepisu nie da
    sie usunac pojedynczo, znikaja razem z przepisem z listy. Bez tego narzedzia
    jedynym sposobem pozbycia sie wlasnej pozycji bylo wyczyszczenie calej
    listy, co jest nieodwracalne.

    Args:
        item_ids: Id pozycji wlasnych, np. z shopping_list -> wlasne_pozycje.
    """
    client = await CLIENT.get()
    wanted = set(item_ids)
    present = {i.id for i in await client.get_additional_items()}
    found = sorted(wanted & present)
    if found:
        await client.remove_additional_items(found)
    return {
        "usunieto": len(found),
        "usuniete_id": found,
        "nieznalezione": sorted(wanted - present),
    }


@mcp.tool(annotations=DESTRUCTIVE)
@_readable
async def clear_shopping_list() -> dict[str, Any]:
    """Wyczysc cala liste zakupow. Nieodwracalne — pytaj uzytkownika przed uzyciem."""
    client = await CLIENT.get()
    await client.clear_shopping_list()
    return {"wyczyszczono": True}


# ---------------------------------------------------------------------------
# kalendarz i kolekcje
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@_readable
async def week_plan(day: str | None = None) -> dict[str, Any]:
    """Plan "moj tydzien" — przepisy zaplanowane w tygodniu zawierajacym dany dzien.

    Args:
        day: Dzien w formacie RRRR-MM-DD. Domyslnie dzisiaj.
    """
    client = await CLIENT.get()
    target = date.fromisoformat(day) if day else date.today()
    days = await client.get_recipes_in_calendar_week(
        target, resolve_custom_recipes=True
    )
    # Zwracane sa tylko dni, na ktore cos zaplanowano — pusty "dni" znaczy
    # pusty tydzien, nie blad. Poczatek tygodnia liczymy sami: wczesniej pole
    # "tydzien_od" odbijalo zapytany dzien, co mylilo.
    monday = target - timedelta(days=target.weekday())
    return {
        "tydzien_od": monday.isoformat(),
        "tydzien_do": (monday + timedelta(days=6)).isoformat(),
        "pytanie_o_dzien": target.isoformat(),
        "dni": _plain(days),
    }


@mcp.tool()
@_readable
async def plan_recipes(
    day: str, recipe_ids: list[str], custom: bool = False
) -> dict[str, Any]:
    """Zaplanuj przepisy na konkretny dzien w kalendarzu Cookidoo.

    Args:
        day: Dzien w formacie RRRR-MM-DD.
        recipe_ids: Id przepisow do zaplanowania.
        custom: True gdy to wlasne przepisy (z my_recipes), False dla Cookidoo.
    """
    client = await CLIENT.get()
    target = date.fromisoformat(day)
    if custom:
        result = await client.add_custom_recipes_to_calendar(target, list(recipe_ids))
    else:
        result = await client.add_recipes_to_calendar(target, list(recipe_ids))
    return {"dzien": target.isoformat(), "plan": _plain(result)}


@mcp.tool(annotations=DESTRUCTIVE)
@_readable
async def unplan_recipes(
    day: str, recipe_ids: list[str], custom: bool = False
) -> dict[str, Any]:
    """Usun przepisy z danego dnia w kalendarzu Cookidoo.

    Odpowiednik plan_recipes. Bez tego zaplanowanego przepisu nie dalo sie
    zdjac z kalendarza przez MCP.

    Args:
        day: Dzien w formacie RRRR-MM-DD.
        recipe_ids: Id przepisow do zdjecia z tego dnia.
        custom: True gdy to wlasne przepisy (z my_recipes), False dla Cookidoo.
    """
    client = await CLIENT.get()
    target = date.fromisoformat(day)
    for recipe_id in recipe_ids:
        if custom:
            await client.remove_custom_recipe_from_calendar(target, recipe_id)
        else:
            await client.remove_recipe_from_calendar(target, recipe_id)
    days = await client.get_recipes_in_calendar_week(
        target, resolve_custom_recipes=True
    )
    pozostalo = next(
        (_plain(d) for d in days if getattr(d, "id", None) == target.isoformat()), None
    )
    return {
        "dzien": target.isoformat(),
        "zdjeto": list(recipe_ids),
        "co_zostalo_na_ten_dzien": pozostalo,
    }


@mcp.tool(annotations=READ_ONLY)
@_readable
async def collections(page: int = 0) -> dict[str, Any]:
    """Kolekcje na koncie: zakupione/zapisane Cookidoo oraz wlasne.

    Args:
        page: Numer strony (0 = pierwsza).
    """
    client = await CLIENT.get()
    managed = await client.get_managed_collections(page)
    custom = await client.get_custom_collections(page)
    managed_total = await client.count_managed_collections()
    custom_total = await client.count_custom_collections()
    return {
        "zapisane": _plain(managed),
        "zapisane_licznik": managed_total,
        "wlasne": _plain(custom),
        "wlasne_licznik": custom_total,
    }


# ---------------------------------------------------------------------------
# prompt: konwersja dowolnego przepisu na przepis Thermomixa
# ---------------------------------------------------------------------------


@mcp.prompt(title="Przerob przepis na Thermomix")
def convert_recipe(recipe: str) -> str:
    """Zamien dowolny przepis (tekst, blog, zdjecie ksiazki) na przepis TM6.

    Args:
        recipe: Tresc przepisu do przerobienia — wklejona albo opisana.
    """
    return f"""Przerob ten przepis na przepis dla Thermomixa {MODEL.value} i wgraj go
na konto Cookidoo uzytkownika.

PRZEPIS ZRODLOWY:
{recipe}

Zasady konwersji:

1. Skladniki — jedna pozycja na linie, zawsze z gramatura w formacie Cookidoo,
   np. "200 g maki pszennej", "2 zabki czosnku", "1 lyzeczka soli".
   Przelicz szklanki i lyzki na gramy. Lacznie maks. 2,2 l objetosci naczynia.

2. Kroki — kazdy krok to jedna czynnosc z konkretnymi ustawieniami TM:
   - czas w SEKUNDACH w polu "time" (5 min -> 300),
   - temperatura w polu "temperature": 37-120 albo "varoma" (gotowanie na parze),
   - obroty w polu "speed": "soft", "0.5"-"10", turbo jako mode
     ("soft" wychodzi na TM jako ikona obrotow delikatnych, nie jako slowo),
   - lewe obroty (delikatne mieszanie, zeby nie rozdrobnic) -> "direction": "CCW",
   - tryby w polu "mode": dough (zagniatanie), browning (podsmazanie,
     na TM6 "Wysokie Temp."), steaming (para), blend (blendowanie), turbo,
     warm_up (podgrzewanie), rice_cooker. Nazwy i ikony trybow dopisuje sam
     serwer — nie wpisuj ich recznie w tresc kroku.

3. Typowe zamiany z klasycznych przepisow:
   - "posiekaj cebule" -> 5 s/obr. 5
   - "zeszklij na patelni" -> 3 min/120°C/obr. 1 (albo mode browning)
   - "gotuj na malym ogniu" -> 100°C/obr. 1, lewe obroty gdy sa kawalki
   - "zmiksuj na krem" -> 1 min/obr. 10, narastajaco od obr. 5
   - "ubij piane / sliwkowa smietane" -> obr. 3.5 z motylkiem
   - "zagnies ciasto" -> 2 min/mode dough
   - "gotuj na parze" -> Varoma/obr. 1, naczynie Varoma w "accessory"
   - duszenie w kawalkach zawsze na lewych obrotach, obr. 1

4. Powiaz skladniki z krokami — pole "ingredients" w kroku:
   {{fragment tekstu kroku: doslowna pozycja z listy skladnikow}}, np.
   {{"oliwe": "20 g oliwy z oliwek", "sol": "sol"}}. Rob to dla KAZDEGO
   skladnika wspomnianego w kroku; bez tego skladniki nie sa podswietlane
   w trakcie prowadzenia. Napis po prawej musi byc identyczny z pozycja
   z listy skladnikow, znak w znak.

5. Nie zmyslaj ustawien, ktorych nie da sie wyprowadzic z przepisu. Gdy krok
   jest czysto reczny ("odstaw na 10 minut", "podawaj z natka"), zostaw sam
   "text" bez ustawien — bedzie krokiem do przeczytania.

6. Frazy ustawien ("3 s/obr. 5", ikona lewych obrotow) serwer buduje sam
   w konwencji Cookidoo — nie dopisuj ich recznie do tekstu kroku.

Kolejnosc dzialan:
  a) wywolaj preview_recipe i pokaz uzytkownikowi wynik: kroki w finalnej
     formie, ile z nich Thermomix poprowadzi, a ile zostanie tekstem,
  b) po akceptacji wywolaj create_recipe,
  c) powiedz, ze przepis pojawi sie w "Moje przepisy" -> "Utworzone przepisy"
     na TM6, gdy urzadzenie bedzie online.
"""


def main() -> None:
    """Uruchom serwer na stdio."""
    logging.basicConfig(
        level=os.getenv("THERMOMIX_LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
