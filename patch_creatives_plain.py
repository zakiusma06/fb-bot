import sys, re
PATH = "/root/fb-bot/telegram-bot/creative_hunt_bot.py"
src = open(PATH).read()
original = src

pat1 = re.compile(
    r'[ \t]*creatives_lines\s*=\s*""\s*\n'
    r'(?:[ \t]+[^\n]+\n)*?'
    r'[ \t]+creatives_lines\s*\+=[^\n]+\n',
    re.MULTILINE
)
m1 = pat1.search(src)
if not m1:
    print("FAIL: creatives_lines block not found"); sys.exit(1)
indent_m = re.search(r'^([ \t]*)creatives_lines', m1.group(0), re.MULTILINE)
pad = indent_m.group(1) if indent_m else "    "
new1 = (pad + '# Show count only; URLs sent as plain follow-up (avoids Entities_too_long).\n'
      + pad + 'creatives_lines = f"\U0001f4da <b>{total_saved}</b> creative(s) saved \u2014 links below\\n" if saved_creatives else ""\n')
src = src[:m1.start()] + new1 + src[m1.end():]
print(f"OK change 1 — creatives_lines replaced")

pat2 = re.compile(
    r'([ \t]*await bot\.send_message\(\s*\n(?:[ \t]+[^\n]+\n)*?[ \t]+disable_web_page_preview=True,\s*\n[ \t]*\)\s*\n)'
    r'([ \t]*except Exception as exc:)',
    re.MULTILINE
)
m2 = pat2.search(src)
if not m2:
    print("FAIL: send_message/except block not found"); sys.exit(1)
p = re.search(r'^([ \t]*)', m2.group(1), re.MULTILINE).group(1)
follow = (
    p+'# Send creative URLs as plain text so Telegram auto-links them.\n'
    +p+'if saved_creatives:\n'
    +p+'    entries = [f"{i+1}. {url}" for i, url in enumerate(saved_creatives)]\n'
    +p+'    header = "Saved creatives \u2014 tap to open:\\n\\n"\n'
    +p+'    chunk, chunk_len = [], 0\n'
    +p+'    for entry in entries:\n'
    +p+'        line = entry + "\\n"\n'
    +p+'        if chunk_len + len(line) > 4000:\n'
    +p+'            await bot.send_message(chat_id=chat_id, text=header+"".join(chunk), disable_web_page_preview=True)\n'
    +p+'            chunk, chunk_len = [], 0\n'
    +p+'        chunk.append(line)\n'
    +p+'        chunk_len += len(line)\n'
    +p+'    if chunk:\n'
    +p+'        await bot.send_message(chat_id=chat_id, text=header+"".join(chunk), disable_web_page_preview=True)\n'
)
src = src[:m2.end(1)] + follow + src[m2.end(1):]
print("OK change 2 — follow-up URL send injected")
open(PATH,"w").write(src)
print("Done")
