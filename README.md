# Thermomix MCP (TM6)

An MCP server for a Cookidoo account. Main goal: **take any recipe (blog, book,
your own idea), convert it into a Thermomix recipe and upload it to a TM6** so
that the device guides you step by step, like with the original recipes, and the
recipe stays saved on your account.

## What it cannot do

The TM6 has **no control API**. There is no local interface and no remote start.
Nobody can start cooking or set the temperature from the outside. Vorwerk does
not expose this, and there is no working workaround.

The closest thing to "control" is what this server does: a recipe with ready
settings lands on the TM6 screen and you only confirm each step. Time,
temperature and speed are already set, so you never type them in by hand.

In addition, a running cook can be **watched** (remote monitoring), but that is
read-only (and see the TM6 note below).

## How it works

```
any recipe  ──►  conversion (model)  ──►  preview_recipe  ──►  create_recipe
                                             (preview)            (upload)
                                                                     │
                     TM6: My recipes ──► Created recipes  ◄──────────┘
```

The recipe goes to your Cookidoo account and syncs to the TM6 when the device is
online. A switched-off Thermomix shows the recipe once it is turned on. Nothing
has to be sent by hand.

### Key detail: "highlighted" settings

Guided cooking for custom recipes works on the TM6 since firmware 3.0, but
**only for settings woven into the step text** (in the Cookidoo editor you select
a piece of text and assign Thermomix settings to it). A step with settings
placed "next to" the text stays plain text to read.

So the server does this automatically: you pass `{"text": "Chop the onion",
"time": 5, "speed": "5"}` and it stores the step as
`Chop the onion 5 s/speed 5` with the annotation attached exactly to the phrase
`5 s/speed 5`. You can also pass `slot` and point at the fragment yourself.

`preview_recipe` shows, before uploading, which steps will be guided and which
will stay plain text.

## Contents

```
server.py            MCP server: 20 tools + the convert_recipe prompt
login.py             one-time login, saves the token
.env.example         configuration template
tests/handshake.py   protocol and step-conversion test, never touches the account
```

Run the test after every change to `server.py`:

```bash
cd "<path>\thermomix-mcp" && ./venv/Scripts/python.exe tests/handshake.py
```

## Installation

```bash
cd "<path>\thermomix-mcp"
python -m venv venv
./venv/Scripts/python.exe -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your Cookidoo account details. Then log
in once. The token is saved in `~/.thermomix-mcp` and the server refreshes it on
its own, so the password does not have to sit in the client configuration:

```bash
./venv/Scripts/python.exe login.py
```

If the password login is rejected by Cloudflare, the script falls back to a
browser mode (it prints a link, you open it, log in and paste back the redirect
address).

## Connecting to Claude

```bash
claude mcp add thermomix "<path>\thermomix-mcp\venv\Scripts\python.exe" "<path>\thermomix-mcp\server.py"
```

Or with an entry in the MCP configuration:

```json
{
  "mcpServers": {
    "thermomix": {
      "command": "<path>\\thermomix-mcp\\venv\\Scripts\\python.exe",
      "args": ["<path>\\thermomix-mcp\\server.py"]
    }
  }
}
```

## Tools

**Account and device**

| | |
|---|---|
| `account_status` | account, subscription, paired Thermomix devices |
| `cooking_status` | what the device is doing now: step, time, temperature (read-only) |

**Recipes**

| | |
|---|---|
| `search_recipes` | search Cookidoo, filtered to TM6 by default |
| `recipe_details` | ingredients, steps, times, nutrition |
| `my_recipes` | list of your own recipes |
| `preview_recipe` | preview of the conversion **without uploading** |
| `create_recipe` | upload a recipe to the account → TM6 |
| `update_recipe` | change an existing own recipe |
| `delete_recipe` | delete (irreversible) |
| `set_recipe_image` | set the cover image of an own recipe |

**Shopping list**

| | |
|---|---|
| `shopping_list` | ingredients, custom items, recipes on the list |
| `add_recipes_to_shopping_list` | add the ingredients of recipes |
| `add_shopping_items` | add custom items |
| `check_off_shopping_items` | check / uncheck |
| `remove_shopping_items` | remove items |
| `clear_shopping_list` | clear everything (irreversible) |

**Planning**

| | |
|---|---|
| `week_plan` | "my week" |
| `plan_recipes` | plan recipes for a day |
| `unplan_recipes` | remove recipes from a day |
| `collections` | saved and own collections |

**Prompt**

`convert_recipe` holds the rules for turning a classic recipe into TM settings
(chop → 5 s/speed 5, sauté → 3 min/120°C/speed 1, simmering pieces → reverse
rotation, and so on), so the conversion is repeatable.

## Step format

```python
{
  "text": "Add the pumpkin and stock, cook",
  "time": 1500,            # SECONDS
  "temperature": 100,      # °C or "varoma"
  "speed": "1",            # "soft", "0.5"–"10"
  "direction": "CCW",      # reverse rotation (gentle stirring)
  "mode": "dough",         # mode name and icon are appended automatically
  "power": "Gentle",       # for browning
  "accessory": "Varoma",   # for steaming
  "ingredients": {"pumpkin": "600 g pumpkin, cut into pieces"},
  "slot": "25 min/100°C/speed 1"   # optional, otherwise generated
}
```

`ingredients` links an ingredient used in the step to the exact entry of the
ingredient list. It is the equivalent of the "link ingredients to steps" stage
in the Cookidoo editor. Without it, ingredients are not highlighted during
guided cooking. The value must be identical to the entry in the recipe's `ingredients` list,
character for character.

A step without any settings (`{"text": "Leave for 10 minutes"}`) stays a step to
read, which is how it should be.

## Notation conventions (derived from real recipes on the account)

The server builds phrases the way Cookidoo does, so do not type them by hand:

| | |
|---|---|
| seconds | `3 s` |
| minutes | `5 min`, with seconds `1 min 30 s` |
| hours | `1 h`, `1 h 35 min` |
| temperature | `120°C`, or `Varoma` |
| reverse rotation | an icon as a separate segment, e.g. `10 min/100°C/<icon>/speed 1` |
| speed | `speed 2`; gentle speed is an icon (not the word "soft") |
| mode | name + icon at the start of the phrase, e.g. `Dough <icon>/2 min` |
| values in the data | a string (`"120"`), not a number |

The exact words depend on the account language (the examples above are
generic). The icons come from the Cookidoo font: in a browser and on the TM6
they render as pictograms, in raw text they are Unicode private-use characters.
The table was built from a scan of 120 platform recipes, not from guesses:

| mode (`mode`) | phrase | source |
|---|---|---|
| `dough` | `Dough <icon>/2 min` | confirmed end to end, shown on a real TM6 |
| `blend` | `Blend <icon>/1 min` | read from platform recipes |
| `warm_up` | `Warm up <icon>/90°C/speed 2` | read from platform recipes |
| `browning` | `High temp. <icon>` | icon read; the TM6 names this mode "High temp." |
| `turbo`, `steaming`, `rice_cooker` | name only, no icon | not found in any recipe |

Besides the modes in the library enum, the platform also uses its own: kettle,
sous-vide, egg cooking, slicing, spiralizing, and the TM7 "open cooking" (where
the icon comes **before** the name). `cookidoo-api` has no keys for them, so they
are not available through MCP for now.

The rule that reproduces every observed variant: `<Name> <icon>`, followed by
plain segments in the order time / temperature / reverse rotation / speed. A set
of 14 platform patterns guards this in `tests/handshake.py`. When one fails,
read the recipe from the source and compare, do not fix it by eye.

Step settings are stored only as an annotation attached to the phrase in the
text. The `settings` field stays empty, exactly as in platform recipes.

## Recipe covers

`set_recipe_image(recipe_id, file_path)` replaces the grey tile with a picture.
It accepts JPG/PNG up to 10 MB, ideally landscape, about 4:3 (the Cookidoo tile
is 584×480 and crops the edges). The image must be a file on disk: the API does
not accept a URL, only a path to a file that is already in Cookidoo storage, so
the tool uploads it there first (like the website editor does) and then attaches
it to the recipe. Steps, ingredients and annotations stay untouched (checked by
comparing the recipe before and after). Keep your cover images out of the
repository.

## Verification status

Checked:

- MCP handshake over stdio, 20 tools and the prompt register,
- login, the token is saved and refreshed on its own,
- reads on a live account: `account_status` (TM6 paired, subscription FULL),
  `my_recipes`, `search_recipes` with the TM6 filter,
- step conversion → guided-cooking annotations, verified **by comparison with a
  real recipe from the account**: the payload comes out structurally identical
  (`INGREDIENT` + `TTS`, positions, temperature as a string, reverse-rotation
  icon),
- annotation offsets with non-ASCII characters and `°C` (counted in UTF-16, they
  hit the exact character),
- readable error messages instead of "Error executing tool".

**Confirmed on a real TM6** (test recipes uploaded to the account):

- the recipe appears in Created recipes on the device,
- the TM6 shows the **START COOKING** button, so it treats the recipe as guided,
  not as plain text,
- steps render with settings: `5 s/speed 5`, `3 min/120°C/speed 1`,
- the **reverse-rotation icon** displays correctly in `20 min/100°C/<icon>/speed 1`,
- **dough mode** shows as "Mode > Dough" with a 2:00 timer, ready to start,
- **gentle speed with reverse rotation** shows both icons in the step text, and
  the manual settings screen shows 10:00 / 100°C with the reverse-rotation icon,
- the **weighing screen** appears with the ingredient list and a TARE button when
  ingredients are linked to the step,
- metadata (TM6, preparation time, total time, servings) and non-ASCII
  characters in ingredients, without distortion,
- `update_recipe` replaces the steps in an existing recipe.

### Remote monitoring does not work on the TM6

`cooking_status` returns an explanation instead of a cooking state. Cooking
monitoring through Cookidoo is a **TM7** feature; on the TM6 the equivalent is
the Thermomix Sensor with the Cooking Center app, which this API does not reach.
`get_monitored_device_ids` returns an empty list even with a TM6 that is on and
online. That is the correct answer, not an error.

### Still unverified

- the remaining modes (`mode`): Vorwerk itself states that not every Thermomix
  mode is supported on import; only `dough` has been confirmed on a device,
- the shopping list, calendar and collection tools (thin wrappers over the
  library, not tested on a live account).

## Disclaimer

Based on the unofficial library [`cookidoo-api`](https://github.com/miaucl/cookidoo-api).
The project is not affiliated with Vorwerk or Cookidoo and may stop working after
changes on their side. Creating custom recipes requires an active Cookidoo 3.0
subscription.
