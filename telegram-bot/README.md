# Meta Ads Research Telegram Bot

An MVP Telegram bot that guides you through a step-by-step extraction workflow, scrapes Meta Ads Library, and saves deduplicated results to Google Sheets.

---

## Features

- Wizard-style Telegram conversation (/extract)
- AI keyword suggestions (no personal API key needed on Replit)
- Meta Ads Library scraping via Playwright
- Filtering by media type and active status
- Deduplication (exact URL match + text similarity + image hashing)
- Google Sheets output + local CSV backup

---

## Setup Guide (Beginner-Friendly)

### 1. Create your Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts — choose a name and username for your bot
4. BotFather will give you a **token** that looks like `123456789:ABCdef...`
5. Copy that token

### 2. Add your bot token to Replit

In Replit, go to the **Secrets** tab (lock icon on the left sidebar) and add:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | Your BotFather token |

### 3. Create a Google Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or use an existing one)
3. Go to **APIs & Services → Library** and enable:
   - **Google Sheets API**
   - **Google Drive API**
4. Go to **APIs & Services → Credentials**
5. Click **Create Credentials → Service Account**
6. Fill in the details and click **Done**
7. Click on the new service account → **Keys** tab → **Add Key → Create new key → JSON**
8. Download the JSON file

### 4. Share your Google Sheet with the service account

1. Open (or create) the Google Sheet where results will be saved
2. Click **Share**
3. Enter the `client_email` from the downloaded JSON file (looks like `something@project.iam.gserviceaccount.com`)
4. Give it **Editor** access
5. Click **Send**

### 5. Add credentials to Replit

In the Replit Secrets tab, add:

| Key | Value |
|-----|-------|
| `GOOGLE_SHEETS_CREDENTIALS_JSON` | Paste the entire JSON file content as one line |
| `GOOGLE_SHEET_NAME` | The exact name of your Google Sheet (e.g. `Meta Ads Research`) |

### 6. Install dependencies

The Replit environment handles this automatically, but if you're running locally:

```bash
pip install -r requirements.txt
playwright install chromium
```

### 7. Run Playwright browser install

Playwright needs the Chromium browser. Run once:

```bash
python -m playwright install chromium
```

In Replit, this is handled automatically when the bot starts.

### 8. Run the bot in Replit

The bot runs via the **Telegram Bot** workflow in Replit. Just hit **Run** or start the workflow.

To run manually:
```bash
python telegram-bot/main.py
```

### 9. Test with /extract

Open your bot in Telegram and send:
```
/extract
```

Follow the wizard:
1. Enter a number (how many products)
2. Choose AI suggestions or type your own keywords
3. Enter countries (e.g. `France, Germany`)
4. Choose media type
5. Choose active status
6. Confirm → sit back and wait!

### 10. Debug in non-headless mode

To see the browser while scraping (useful for debugging), set in your `.env` or Replit Secrets:

```
HEADLESS=false
```

This opens a visible browser window. Note: VNC/display server required for non-headless mode on Linux servers.

---

## Project Structure

```
telegram-bot/
├── main.py              # Entry point
├── bot.py               # Telegram bot setup and commands
├── conversation.py      # Wizard conversation flow
├── ads_scraper.py       # Playwright Meta Ads Library scraper
├── product_extractor.py # Landing page title enrichment
├── deduplicator.py      # Text + image deduplication
├── sheet_writer.py      # Google Sheets read/write
├── ai_keywords.py       # AI keyword suggestions
├── config.py            # Configuration from env vars
├── utils.py             # Shared helpers
├── requirements.txt     # Python dependencies
└── backups/             # Auto-created CSV backup folder
```

---

## Configuration

All settings live in environment variables / Replit Secrets:

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | Required. Your bot token from BotFather |
| `GOOGLE_SHEETS_CREDENTIALS_JSON` | — | Required. Service account JSON (full content) |
| `GOOGLE_SHEET_NAME` | `Meta Ads Research` | Name of your Google Sheet |
| `HEADLESS` | `true` | Run browser headlessly |
| `MAX_ADS_TO_SCAN_PER_KEYWORD` | `50` | Max ads to scan per keyword+country combo |
| `TEXT_DEDUP_THRESHOLD` | `85` | Text similarity threshold (0-100) |
| `IMAGE_DEDUP_THRESHOLD` | `10` | Perceptual hash distance threshold |

---

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/extract` | Start a new extraction wizard |
| `/status` | Check bot status |
| `/help` | Show usage guide |
| `/cancel` | Cancel the current operation |

---

## Output

Results are saved to your Google Sheet with these columns:

`keyword`, `country`, `advertiser_name`, `ad_library_url`, `landing_page_url`, `ad_text`, `media_type`, `media_url`, `thumbnail_url`, `extracted_product_name`, `normalized_product_name`, `main_image_url`, `page_title`, `duplicate_group_id`, `duplicates_count`, `active_status`, `status`, `created_at`

A CSV backup is also saved locally in `telegram-bot/backups/`.

---

## TODO (Future upgrades)

- [ ] Webhook mode for production deployments (instead of polling)
- [ ] Proxy support for scraping from restricted regions  
- [ ] AI-powered product name extraction from ad text
- [ ] Scheduled automatic extractions
- [ ] Dashboard web UI for viewing results
- [ ] More advanced image similarity (CLIP embeddings)
- [ ] Multi-user support with per-user sheet isolation
