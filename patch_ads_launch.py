import sys, re

PATH = "/root/fb-bot/telegram-bot/ads_launch_bot.py"
src = open(PATH).read()
original = src

def _build_follow_up(pad):
    return (
        '\n'
        + pad + '# Send creative URLs as plain text — Telegram auto-links them, no entity limits.\n'
        + pad + 'if s["creative_urls"]:\n'
        + pad + '    bot = update.get_bot()\n'
        + pad + '    chat_id = (update.callback_query or update).message.chat_id\n'
        + pad + '    entries = [f"{i+1}. {u}" for i, u in enumerate(s["creative_urls"])]\n'
        + pad + '    header = "Creatives — tap to open:\\n\\n"\n'
        + pad + '    chunk, chunk_len = [], 0\n'
        + pad + '    for entry in entries:\n'
        + pad + '        line = entry + "\\n"\n'
        + pad + '        if chunk_len + len(line) > 4000:\n'
        + pad + '            await bot.send_message(chat_id=chat_id,\n'
        + pad + '                                   text=header + "".join(chunk),\n'
        + pad + '                                   disable_web_page_preview=True)\n'
        + pad + '            chunk, chunk_len = [], 0\n'
        + pad + '        chunk.append(line)\n'
        + pad + '        chunk_len += len(line)\n'
        + pad + '    if chunk:\n'
        + pad + '        await bot.send_message(chat_id=chat_id,\n'
        + pad + '                               text=header + "".join(chunk),\n'
        + pad + '                               disable_web_page_preview=True)\n'
    )

pat1 = re.compile(
    r'[ \t]*creatives_text\s*=\s*""\s*\n'
    r'[ \t]+if\s+s\["creative_urls"\]:\s*\n'
    r'[ \t]+lines\s*=\s*\[.*?\]\s*\n'
    r'[ \t]+creatives_text\s*=\s*"\\n<b>Creatives:</b>\\n"[^\n]+\n'
    r'[ \t]+else:\s*\n'
    r'[ \t]+creatives_text\s*=\s*"\\n⚠️[^\n]+"\s*\n',
    re.MULTILINE,
)
m1 = pat1.search(src)
if not m1:
    print("FAIL: could not locate creatives_text block in _show_product"); sys.exit(1)
indent_m = re.search(r'^([ \t]*)creatives_text', m1.group(0), re.MULTILINE)
pad = indent_m.group(1) if indent_m else "    "
new1 = (
    pad + '# Show count only in card; URLs sent as plain follow-up (avoids Entities_too_long).\n'
    + pad + 'creatives_text = (\n'
    + pad + '    f"\\n📚 <b>{len(s[\'creative_urls\'])}</b> creative(s) — links below\\n"\n'
    + pad + '    if s["creative_urls"] else "\\n⚠️ No creatives found"\n'
    + pad + ')\n'
)
src = src[:m1.start()] + new1 + src[m1.end():]
print("OK: Change 1 — creatives_text block replaced")

pat2 = re.compile(r'([ \t]*await _reply\(update,\s*text,\s*kb,\s*parse_mode=ParseMode\.HTML\)\n)', re.MULTILINE)
all_m2 = list(pat2.finditer(src))
if not all_m2:
    print("FAIL: could not find await _reply(...ParseMode.HTML)"); sys.exit(1)
m2 = all_m2[0]
inner_pad = re.search(r'^([ \t]*)', m2.group(1), re.MULTILINE).group(1)
src = src[:m2.end()] + _build_follow_up(inner_pad) + src[m2.end():]
print("OK: Change 2 — follow-up injected after _reply(...HTML)")

pat3 = re.compile(
    r'([ \t]*)count\s*=\s*len\(s\["selected_urls"\]\)\s*\n'
    r'[ \t]*#[^\n]*\n'
    r'[ \t]*text\s*=\s*\(\s*\n'
    r'(?:[ \t]+[^\n]+\n)*?'
    r'[ \t]+\+\s*"\\n"\.join\([^\n]+s\["creative_urls"\][^\n]+\)\s*\n'
    r'[ \t]*\)\s*\n'
    r'([ \t]*await _reply\(update,\s*text,\s*rows,\s*parse_mode=None\)\n)',
    re.MULTILINE,
)
m3 = pat3.search(src)
if not m3:
    print("FAIL: could not locate _show_creative_select text block"); sys.exit(1)
pad3 = m3.group(1)
new3 = (
    pad3 + 'count = len(s["selected_urls"])\n'
    + pad3 + 'total = len(s["creative_urls"])\n'
    + pad3 + '# URLs omitted from card — sent as plain follow-up to avoid message-too-long errors.\n'
    + pad3 + 'text = (\n'
    + pad3 + '    f"🎬 Select creatives to use\\n\\n"\n'
    + pad3 + '    f"Tap to toggle. {count} selected.\\n"\n'
    + pad3 + '    f"1 creative → Normal ad\\n"\n'
    + pad3 + '    f"2+ creatives → Flexible/dynamic ad\\n\\n"\n'
    + pad3 + '    f"{total} creative(s) — full links below ↓"\n'
    + pad3 + ')\n'
    + m3.group(2)
    + '\n'
    + pad3 + '# Send full URLs as plain text — Telegram auto-links them, no entity limits.\n'
    + pad3 + 'bot = update.get_bot()\n'
    + pad3 + 'chat_id = (update.callback_query or update).message.chat_id\n'
    + pad3 + 'entries = [f"{i+1}. {u}" for i, u in enumerate(s["creative_urls"])]\n'
    + pad3 + 'header = "Creatives — tap to open:\\n\\n"\n'
    + pad3 + 'chunk, chunk_len = [], 0\n'
    + pad3 + 'for entry in entries:\n'
    + pad3 + '    line = entry + "\\n"\n'
    + pad3 + '    if chunk_len + len(line) > 4000:\n'
    + pad3 + '        await bot.send_message(chat_id=chat_id,\n'
    + pad3 + '                               text=header + "".join(chunk),\n'
    + pad3 + '                               disable_web_page_preview=True)\n'
    + pad3 + '        chunk, chunk_len = [], 0\n'
    + pad3 + '    chunk.append(line)\n'
    + pad3 + '    chunk_len += len(line)\n'
    + pad3 + 'if chunk:\n'
    + pad3 + '    await bot.send_message(chat_id=chat_id,\n'
    + pad3 + '                           text=header + "".join(chunk),\n'
    + pad3 + '                           disable_web_page_preview=True)\n'
)
src = src[:m3.start()] + new3 + src[m3.end():]
print("OK: Change 3 — _show_creative_select replaced + follow-up injected")

if src == original:
    print("FAIL: no changes were made"); sys.exit(1)
open(PATH, "w").write(src)
print(f"\nDone → {PATH}")
