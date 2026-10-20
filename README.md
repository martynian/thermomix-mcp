# Thermomix MCP (TM6)

Serwer MCP do konta Cookidoo. Główny cel: **wziąć dowolny przepis (blog, książka,
własna głowa), przerobić go na przepis Thermomixa i wgrać na TM6** tak, żeby
urządzenie prowadziło krok po kroku — jak przy oryginalnych przepisach — i żeby
został zapisany na koncie.

## Czego nie da się zrobić

TM6 **nie ma API do sterowania**. Nie ma lokalnego interfejsu, nie ma zdalnego
startu. Nikt nie wystartuje gotowania ani nie ustawi temperatury z zewnątrz —
Vorwerk tego nie udostępnia i nie istnieje działający obejście.

Najbliżej „sterowania" jest to, co robi ten serwer: przepis z gotowymi
ustawieniami ląduje na ekranie TM6, a Ty tylko potwierdzasz kolejne kroki.
Czas, temperatura i obroty są już ustawione — nie wklepujesz ich ręcznie.

Dodatkowo da się **podejrzeć** trwające gotowanie (remote monitoring), ale to
tylko odczyt.

## Jak to działa

```
dowolny przepis  ──►  konwersja (model)  ──►  preview_recipe  ──►  create_recipe
                                                  (podgląd)          (wgranie)
                                                                         │
                       TM6: Moje przepisy ──► Utworzone przepisy  ◄──────┘
```

Przepis trafia na konto Cookidoo i synchronizuje się z TM6, gdy urządzenie jest
online. Wyłączony Thermomix zobaczy przepis po włączeniu — nic nie trzeba
wysyłać ręcznie.

### Kluczowy szczegół: „podświetlone" ustawienia

Guided cooking dla własnych przepisów działa na TM6 od oprogramowania 3.0, ale
**tylko dla ustawień wplecionych w treść kroku** (w edytorze Cookidoo robi się
to, zaznaczając fragment tekstu i przypisując ustawienia Thermomixa). Krok z
ustawieniami „obok" tekstu zostaje zwykłym tekstem do przeczytania.

Dlatego serwer robi to automatycznie: podajesz `{"text": "Posiekaj cebulę",
"time": 5, "speed": "5"}`, a on zapisuje krok jako
`Posiekaj cebulę — 5 s/obr. 5` z adnotacją przypiętą dokładnie do frazy
`5 s/obr. 5`. Możesz też podać `slot` i wskazać fragment samodzielnie.

`preview_recipe` pokazuje przed wgraniem, które kroki będą prowadzone, a które
zostaną tekstem.

## Zawartość

```
server.py            serwer MCP — 20 narzędzi + prompt convert_recipe
login.py             jednorazowe logowanie, zapisuje token
.env.example         wzór konfiguracji
tests/handshake.py   test protokołu i konwersji kroków, bez dotykania konta
```

Test po każdej zmianie w `server.py`:

```bash
cd "<sciezka>\thermomix-mcp" && ./venv/Scripts/python.exe tests/handshake.py
```

## Instalacja

```bash
cd "<sciezka>\thermomix-mcp"
python -m venv venv
./venv/Scripts/python.exe -m pip install -r requirements.txt
```

Skopiuj `.env.example` do `.env` i wpisz dane konta Cookidoo. Potem zaloguj się
raz — token zapisze się w `~/.thermomix-mcp` i serwer będzie go sam odświeżał,
więc hasło nie musi siedzieć w konfiguracji Claude'a:

```bash
./venv/Scripts/python.exe login.py
```

Gdy logowanie hasłem odbije się od Cloudflare, skrypt przechodzi w tryb
przeglądarkowy (wypisuje link, Ty wklejasz adres przekierowania).

## Podłączenie do Claude'a

```bash
claude mcp add thermomix "<sciezka>\thermomix-mcp\venv\Scripts\python.exe" "<sciezka>\thermomix-mcp\server.py"
```

Albo wpisem w konfiguracji MCP:

```json
{
  "mcpServers": {
    "thermomix": {
      "command": "<sciezka>\\thermomix-mcp\\venv\\Scripts\\python.exe",
      "args": ["<sciezka>\\thermomix-mcp\\server.py"]
    }
  }
}
```

## Narzędzia

**Konto i urządzenie**

| | |
|---|---|
| `account_status` | konto, subskrypcja, sparowane Thermomixy |
| `cooking_status` | co TM robi teraz: krok, czas, temperatura (tylko odczyt) |

**Przepisy**

| | |
|---|---|
| `search_recipes` | szukanie w Cookidoo, domyślnie filtr na TM6 |
| `recipe_details` | składniki, kroki, czasy, wartości odżywcze |
| `my_recipes` | lista własnych przepisów |
| `preview_recipe` | podgląd konwersji **bez wgrywania** |
| `create_recipe` | wgranie przepisu na konto → TM6 |
| `update_recipe` | zmiana istniejącego własnego przepisu |
| `delete_recipe` | usunięcie (nieodwracalne) |

**Lista zakupów**

| | |
|---|---|
| `shopping_list` | składniki, własne pozycje, przepisy na liście |
| `add_recipes_to_shopping_list` | dorzuć składniki przepisów |
| `add_shopping_items` | dorzuć własne pozycje |
| `check_off_shopping_items` | odhacz / odznacz |
| `clear_shopping_list` | wyczyść wszystko (nieodwracalne) |

**Planowanie**

| | |
|---|---|
| `week_plan` | „mój tydzień" |
| `plan_recipes` | zaplanuj przepisy na dzień |
| `collections` | kolekcje zapisane i własne |

**Prompt**

`convert_recipe` — reguły przerabiania klasycznego przepisu na ustawienia TM
(posiekaj → 5 s/obr. 5, zeszklij → 3 min/120°C/obr. 1, duszenie w kawałkach →
lewe obroty itd.), żeby konwersja była powtarzalna.

## Format kroku

```python
{
  "text": "Dodaj dynię i bulion, gotuj",
  "time": 1500,            # SEKUNDY
  "temperature": 100,      # °C albo "varoma"
  "speed": "1",            # "soft", "0.5"–"10"
  "direction": "CCW",      # lewe obroty (delikatne mieszanie)
  "mode": "dough",         # nazwa i ikona trybu dopisuja sie same
  "power": "Gentle",       # dla browning
  "accessory": "Varoma",   # dla steaming
  "ingredients": {"dynię": "600 g dyni, pokrojonej na kawałki"},
  "slot": "25 min/100°C/obr. 1"   # opcjonalnie — inaczej dopisze się sam
}
```

`ingredients` wiąże składnik użyty w kroku z dokładną pozycją z listy — to
odpowiednik etapu „połącz składniki z krokami" w edytorze Cookidoo. Bez tego
składniki nie są podświetlane podczas prowadzenia. Napis po prawej musi być
identyczny z pozycją z listy `ingredients` przepisu, znak w znak.

Krok bez żadnych ustawień (`{"text": "Odstaw na 10 minut"}`) zostaje krokiem do
przeczytania — i tak powinno być.

## Konwencje zapisu (ustalone z prawdziwych przepisów na koncie)

Serwer buduje frazy tak, jak robi to Cookidoo — nie wpisuj ich ręcznie:

| | |
|---|---|
| sekundy | `3 s` (nie „3 sek") |
| minuty | `5 min`, z sekundami `1 min 30 s` |
| godziny | `1 godz.`, `1 godz. 35 min` — z kropką |
| temperatura | `120°C`, albo `Varoma` |
| lewe obroty | ikona `` jako osobny segment: `10 min/100°C//obr. 1` |
| obroty | `obr. 2`; obroty delikatne jako ikona: `obr. ` (nie „soft") |
| tryb | nazwa + ikona na początku frazy: `Ciasto /2 min` |
| stopnie w danych | napis (`"120"`), nie liczba |

Ikony pochodzą z fontu Cookidoo — w przeglądarce i na TM6 renderują się jako
piktogramy, w surowym tekście to znaki z obszaru prywatnego Unicode. Tablica
powstała ze skanu 120 przepisów platformy, nie z domysłów:

| tryb (`mode`) | fraza | źródło |
|---|---|---|
| `dough` | `Ciasto /2 min` | potwierdzony do końca — adnotacja `mode="dough"` leży na koncie |
| `blend` | `Miksowanie /1 min` | odczytany z przepisów platformy |
| `warm_up` | `Podgrzewanie /90°C/obr. 2` | odczytany z przepisów platformy |
| `browning` | `Wysokie Temp. ` | ikona odczytana; TM6 nazywa ten tryb „Wysokie Temp." |
| `turbo`, `steaming`, `rice_cooker` | sama nazwa, bez ikony | nie znalezione w żadnym przepisie |

Poza trybami z enuma biblioteki platforma używa też własnych: `Czajnik /100°C`,
`Sous-vide /15 min/55°C`, `Gotowanie Jajek /miękkie`, `Krojenie /Grubo`,
`Spiralizowanie ` oraz TM7-owe ` Otwarte Gotowanie/3 min/100°C` (tam ikona
stoi **przed** nazwą). `cookidoo-api` nie ma dla nich kluczy, więc przez MCP są
na razie niedostępne.

Reguła, która odtwarza wszystkie zaobserwowane warianty: `<Nazwa> <ikona>`, a po
niej zwykłe segmenty w kolejności czas / temperatura / lewe obroty / obroty.
Zestaw 14 wzorców z platformy pilnuje tego w `tests/handshake.py` — gdy któryś
padnie, przeczytaj przepis ze źródła i porównaj, nie poprawiaj „na oko".

Ustawienia kroku trzymane są wyłącznie jako adnotacja przypięta do frazy w
tekście — pole `settings` zostaje puste, dokładnie jak w przepisach platformy.

## Okładki przepisów

`set_recipe_image(recipe_id, file_path)` zamienia szary kafelek na zdjęcie.
Przyjmuje JPG/PNG do 10 MB; najlepiej poziomy, proporcje ok. 4:3 (kafelek
Cookidoo ma 584×480 i przycina brzegi). Obraz musi leżeć na dysku — API nie
przyjmuje adresu URL, tylko ścieżkę do pliku, który już jest w magazynie
Cookidoo, więc narzędzie najpierw go tam wgrywa (tak jak edytor na stronie),
a potem podpina do przepisu. Kroki, składniki i adnotacje zostają nietknięte
(sprawdzone porównaniem przepisu przed i po). Okładki trzymaj poza repozytorium.

## Status weryfikacji

Sprawdzone:

- handshake MCP po stdio, 20 narzędzi i prompt się rejestrują,
- logowanie, token zapisuje się i odświeża sam,
- odczyty na żywym koncie: `account_status` (TM6 sparowany, subskrypcja FULL),
  `my_recipes`, `search_recipes` z filtrem TM6,
- konwersja kroków → adnotacje guided cooking, zweryfikowana **przez porównanie
  z prawdziwym przepisem z konta**: payload wychodzi strukturalnie identyczny
  (`INGREDIENT` + `TTS`, pozycje, temperatura jako napis, ikona lewych obrotów),
- offsety adnotacji przy polskich znakach i `°C` (liczone w UTF-16, trafiają
  co do znaku),
- czytelne komunikaty błędów zamiast „Error executing tool".

**Potwierdzone na prawdziwym TM6** (wgrany przepis testowy):

- przepis pojawia się w Utworzonych przepisach na urządzeniu,
- TM6 pokazuje przycisk **ROZPOCZNIJ GOTOWANIE** — czyli uznaje przepis za
  prowadzony, nie tekstowy,
- kroki renderują się z ustawieniami: `5 s/obr. 5`, `3 min/120°C/obr. 1`,
- **ikona lewych obrotów (``) wyświetla się poprawnie** w
  `20 min/100°C/⟲/obr. 1`,
- metadane (TM6, czas przygotowania, czas całkowity, porcje) i polskie znaki
  w składnikach — bez zniekształceń,
- `update_recipe` podmienia kroki w istniejącym przepisie.

### Remote monitoring nie działa na TM6

`cooking_status` zwróci wyjaśnienie zamiast stanu gotowania. Podgląd gotowania
przez Cookidoo to funkcja **TM7**; na TM6 odpowiednikiem jest czujnik Thermomix
Sensor z aplikacją Cooking Center, do których to API nie sięga.
`get_monitored_device_ids` zwraca pustą listę nawet przy włączonym, online TM6 —
to prawidłowa odpowiedź, nie błąd.

### Nadal niesprawdzone

- które tryby (`mode`) przechodzą — Vorwerk sam zastrzega, że nie wszystkie
  tryby Thermomixa są obsługiwane przy imporcie; przepis testowy ich nie używał,
- narzędzia listy zakupów, kalendarza i kolekcji (cienkie opakowania na
  bibliotekę, ale nieprzetestowane na żywym koncie).

## Zastrzeżenie

Oparte na nieoficjalnej bibliotece [`cookidoo-api`](https://github.com/miaucl/cookidoo-api).
Projekt nie jest powiązany z Vorwerk ani Cookidoo i może przestać działać po
zmianach po ich stronie. Tworzenie własnych przepisów wymaga aktywnej
subskrypcji Cookidoo 3.0.
