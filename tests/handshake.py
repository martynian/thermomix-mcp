"""Test protokolu MCP po stdio — bez dotykania konta Cookidoo.

Odpala server.py jako podproces, przeprowadza handshake i sprawdza, czy
narzedzia i prompt sa zarejestrowane. Sprawdza tez, czy frazy ustawien wychodza
znak w znak takie jak w przepisach platformy Cookidoo. Przydaje sie po kazdej
zmianie w server.py i do diagnozy, gdy Claude nie widzi serwera.

    .\\venv\\Scripts\\python.exe tests\\handshake.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
SERVER = ROOT / "server.py"
TIMEOUT = 60


# Frazy odczytane z prawdziwych przepisow Cookidoo (skan 120 przepisow z konta
# plus trzy przepisy Vorwerka lezace we "Moich przepisach"). Kazda z nich musi
# wyjsc z serwera znak w znak, razem z ikonami fontu — inaczej TM6 nie poprowadzi
# kroku tak jak przy przepisach platformy. Nie poprawiac "na oko": gdy ktorys
# przypadek padnie, przeczytac przepis ze zrodla i porownac.
PHRASES: list[tuple[str, object, object, object, object, object]] = [
    # (oczekiwana fraza, czas_s, temperatura, obroty, kierunek, tryb)
    ("5 s/obr. 5", 5, None, "5", None, None),
    ("3 min/120°C/obr. 1", 180, 120, "1", None, None),
    ("20 min/100°C//obr. 1", 1200, 100, "1", "CCW", None),
    ("4 s//obr. 4", 4, None, "4", "CCW", None),
    ("5 min/120°C//obr. ", 300, 120, "soft", "CCW", None),
    ("5 min/120°C/obr. ", 300, 120, "soft", None, None),
    ("20 min/Varoma//obr. 1", 1200, "Varoma", "1", "CCW", None),
    ("100°C//obr. 1", None, 100, "1", "CCW", None),
    ("Ciasto /2 min", 120, None, None, None, "dough"),
    ("Miksowanie /1 min", 60, None, None, None, "blend"),
    ("Miksowanie /1 min 30 s", 90, None, None, None, "blend"),
    ("Podgrzewanie /90°C/obr. 2", None, 90, "2", None, "warm_up"),
    ("Wysokie Temp. ", None, None, None, None, "browning"),
    ("1 godz. 35 min/95°C", 5700, 95, None, None, None),
]


def _check_phrases() -> int:
    """Porownaj frazy generowane przez serwer z frazami platformy."""
    sys.path.insert(0, str(ROOT))
    import server

    bad = 0
    for expected, time_s, temp, speed, direction, mode in PHRASES:
        got = server._settings_phrase(time_s, temp, speed, direction, mode)
        if got != expected:
            bad += 1
            print(f"BLAD: fraza {expected!r} wyszla jako {got!r}")
    if bad == 0:
        print(f"OK  frazy TM     {len(PHRASES)} wzorcow z platformy, zgodne")
    return bad


async def _run() -> int:
    if not PYTHON.exists():
        print(f"Brak venv: {PYTHON}", file=sys.stderr)
        return 1

    failures_phrases = _check_phrases()

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = await asyncio.create_subprocess_exec(
        str(PYTHON),
        str(SERVER),
        cwd=str(ROOT),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def send(message: dict[str, object]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def receive() -> dict:
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), TIMEOUT)
        if not line:
            raise RuntimeError("serwer zamknal wyjscie przed odpowiedzia")
        return json.loads(line)

    failures = failures_phrases
    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "handshake-test", "version": "0"},
                },
            }
        )
        result = (await receive())["result"]
        info = result["serverInfo"]
        print(f"OK  initialize    {info['name']} v{info.get('version')}")
        print(f"    protokol      {result['protocolVersion']}")

        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = (await receive())["result"]["tools"]
        print(f"OK  tools/list    {len(tools)} narzedzi")
        if len(tools) < 20:
            print(f"BLAD: spodziewano sie co najmniej 20, jest {len(tools)}")
            failures += 1

        await send({"jsonrpc": "2.0", "id": 3, "method": "prompts/list"})
        prompts = [p["name"] for p in (await receive())["result"]["prompts"]]
        print(f"OK  prompts/list  {prompts}")
        if "convert_recipe" not in prompts:
            print("BLAD: brak promptu convert_recipe")
            failures += 1

        # Konwersja krokow — bez sieci, wiec dziala nawet bez zalogowania.
        await send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "preview_recipe",
                    "arguments": {
                        "name": "Test",
                        "ingredients": ["80 g cebuli"],
                        "steps": [
                            {
                                "text": "Włożyć cebulę, rozdrobnić",
                                "time": 5,
                                "speed": "5",
                                "ingredients": {"cebulę": "80 g cebuli"},
                            },
                            {"text": "Odstawić na 5 minut."},
                        ],
                    },
                },
            }
        )
        reply = (await receive())["result"]
        if reply.get("isError"):
            print(f"BLAD: preview_recipe zwrocil blad: {reply}")
            failures += 1
        else:
            data = reply["structuredContent"]
            krok = data["kroki"][0]
            print(f"OK  konwersja     {krok['tekst']!r}")
            print(f"    skladniki     {krok['skladniki_powiazane']}")
            if "5 s/obr. 5" not in krok["tekst"]:
                print("BLAD: fraza ustawien nie trafila do tekstu kroku")
                failures += 1
            if data["krokow_prowadzonych"] != 1 or data["krokow_tekstowych"] != 1:
                print("BLAD: zly podzial na kroki prowadzone i tekstowe")
                failures += 1
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()

    print()
    print("WSZYSTKO OK" if failures == 0 else f"BLEDOW: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
