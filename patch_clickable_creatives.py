import sys
PATH = "/root/fb-bot/telegram-bot/creative_hunt_bot.py"
src = open(PATH).read()
original = src

OLD = (
    "    # List each saved creative as a numbered line (truncated to avoid msg-too-long)\n"
    "    creatives_lines = \"\"\n"
    "    for i, url in enumerate(saved_creatives):\n"
    "        short = (url[:55] + \"\u2026\") if len(url) > 55 else url\n"
    "        creatives_lines += f\"\ud83d\udcda <b>{i+1}/{total_saved}</b> {_esc(short)}\\n\"\n"
)
NEW = (
    "    # Clickable link per creative \u2014 works for both Facebook Library and CDN media URLs.\n"
    "    creatives_lines = \"\"\n"
    "    for i, url in enumerate(saved_creatives):\n"
    "        safe_url = url.replace(\"&\", \"&amp;\").replace('\"', \"&quot;\")\n"
    "        creatives_lines += f'\ud83d\udcda <b>{i+1}/{total_saved}</b> <a href=\"{safe_url}\">\ud83d\udd17 View Creative</a>\\n'\n"
)

if OLD not in src:
    print("FAIL: old pattern not found — checking file...")
    for j, line in enumerate(src.splitlines(), 1):
        if "creatives_lines" in line or "_esc(short)" in line:
            print(f"  line {j}: {line}")
    sys.exit(1)

src = src.replace(OLD, NEW, 1)
open(PATH, "w").write(src)
print("OK: creatives_lines updated to clickable <a href> links")
