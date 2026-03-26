import sys, re
PATH = "/root/fb-bot/telegram-bot/creative_hunt_bot.py"
src = open(PATH).read()
original = src

# Find and replace the creative_rows block containing url=url
pattern = re.compile(
    r'([ \t]*)creative_rows\s*=\s*\[\]\s*\n'
    r'(?:[ \t]*.*\n)*?'
    r'[ \t]*\]\)\n',
    re.MULTILINE
)
m = pattern.search(src)
if not m:
    print("FAIL: creative_rows block not found")
    sys.exit(1)

indent = m.group(1)
replacement = (
    indent + "creative_rows = [\n" +
    indent + "    [InlineKeyboardButton(f\"\\U0001f5d1 Remove #{i+1}\", callback_data=f\"ch_del_creative:{sku}:{i}\")]\n" +
    indent + "    for i in range(len(saved_creatives))\n" +
    indent + "]\n"
)
src = src[:m.start()] + replacement + src[m.end():]
if src == original:
    print("FAIL: no change made")
    sys.exit(1)

open(PATH, "w").write(src)
print("OK: creative_rows block replaced")
print("Verify with: grep -n 'Remove #\\|url=url' " + PATH)
