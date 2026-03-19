# Workspace

## Overview

pnpm workspace monorepo (TypeScript) + Python Telegram Bot for Meta Ads Research.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)
- **Python**: 3.11 (for Telegram bot)

## Structure

```text
artifacts-monorepo/
├── artifacts/              # Deployable applications
│   └── api-server/         # Express API server
├── lib/                    # Shared libraries
│   ├── api-spec/           # OpenAPI spec + Orval codegen config
│   ├── api-client-react/   # Generated React Query hooks
│   ├── api-zod/            # Generated Zod schemas from OpenAPI
│   └── db/                 # Drizzle ORM schema + DB connection
├── scripts/                # Utility scripts (single workspace package)
├── telegram-bot/           # Python Telegram bot (Meta Ads Research)
│   ├── main.py             # Entry point
│   ├── bot.py              # Bot setup and commands
│   ├── conversation.py     # Wizard conversation flow
│   ├── ads_scraper.py      # Playwright Meta Ads Library scraper
│   ├── product_extractor.py # Landing page enrichment
│   ├── deduplicator.py     # Text + image deduplication
│   ├── sheet_writer.py     # Google Sheets read/write
│   ├── ai_keywords.py      # AI keyword suggestions
│   ├── config.py           # Config from env vars
│   ├── utils.py            # Shared helpers
│   └── requirements.txt    # Python dependencies
├── pnpm-workspace.yaml     # pnpm workspace config
├── tsconfig.base.json      # Shared TS options
├── tsconfig.json           # Root TS project references
└── package.json            # Root package
```

## Telegram Bot System

### Bot Architecture

| Bot | Workflow | Entry point | Token secret | Source tab | Dest tab(s) |
|-----|----------|-------------|--------------|------------|-------------|
| Product Research Bot | `Telegram Bot` | `main.py` | `TELEGRAM_BOT_TOKEN` | — | PENDING |
| **Approval Bot** | `Approval Bot` | `approval_main.py` | `TELEGRAM_APPROVAL_BOT_TOKEN` | PENDING | APPROVED / DISAPPROVED |
| Creative Hunt Bot | `Creative Hunt Bot` | `creative_hunt_main.py` | `TELEGRAM_CREATIVE_BOT_TOKEN` | APPROVED | READY FOR ADS |
| Scheduler Bot | `Scheduler Bot` | `scheduler_main.py` | `TELEGRAM_SCHEDULER_BOT_TOKEN` | — | — |
| Ads Launch Bot | `Ads Launch Bot` | `ads_launch_main.py` | `TELEGRAM_ADS_LAUNCH_BOT_TOKEN` | — | — |

> **Deprecated** (do not restart): `mod_main.py` (Moderation Bot), `pricing_main.py` (Pricing Bot).
> These are replaced by `approval_main.py`.

### Approval Bot flow (approval_main.py / approval_bot.py)
1. Load products from PENDING (with keyword/price/variant filters)
2. Product card shows: keyword, product URL, ad creative URL, sourcing price, supplier link, weight, has variants, suggested selling price (live formula)
3. Edit SC Price (RMB→USD), Edit Weight → both refresh suggested price and save to PENDING immediately
4. Enter Selling Price + Compare At Price → saved immediately to PENDING
5. 🚀 PREAPPROVE (blocked unless both prices set) → runs Shopify pipeline
6. After Shopify creation: shows storefront link + ✅ Approve Product | 🔄 Regenerate | ⏭ Skip
7. Regenerate: change title / description / delete images
8. Approve Product → PENDING → APPROVED
9. Disapprove → PENDING → DISAPPROVED
10. Skip → stays in PENDING

### Product Research Bot
- **Workflow**: `Telegram Bot`
- **Command**: `cd /home/runner/workspace/telegram-bot && python main.py`

### Required Secrets
- `TELEGRAM_BOT_TOKEN` — Research Bot
- `TELEGRAM_APPROVAL_BOT_TOKEN` — Approval Bot
- `TELEGRAM_CREATIVE_BOT_TOKEN` — Creative Hunt Bot
- `TELEGRAM_SCHEDULER_BOT_TOKEN` — Scheduler Bot
- `TELEGRAM_ADS_LAUNCH_BOT_TOKEN` — Ads Launch Bot
- `GOOGLE_SHEETS_CREDENTIALS_JSON` — full service account JSON content
- `GOOGLE_SHEET_NAME` — name of target Google Sheet

### Optional Settings
- `HEADLESS` (default: `true`) — browser headless mode
- `MAX_ADS_TO_SCAN_PER_KEYWORD` (default: `50`)
- `TEXT_DEDUP_THRESHOLD` (default: `85`)
- `IMAGE_DEDUP_THRESHOLD` (default: `10`)

### Bot Commands
- `/start` — welcome
- `/extract` — start extraction wizard
- `/status` — bot status
- `/help` — usage guide
- `/cancel` — cancel current operation

### Playwright
Chromium is installed at `.cache/ms-playwright/`. Run `python -m playwright install chromium` to reinstall if needed.

### Troubleshooting: Facebook Session Expired
**Symptoms:**
- Bot finds 0 ads or "All N ads had no landing page URL" — especially for EU countries (France, etc.)
- Logs show `[resolve] redirected off Facebook for ad {id} → metastatus.com/ads-transparency`
- You can see ads and links manually in your browser but the bot cannot

**Why it happens:** Facebook redirects individual ad pages to `metastatus.com/ads-transparency` for unauthenticated sessions. The bot's saved auth state (`fb_auth_state.json`) uses cookies from `FACEBOOK_COOKIES` secret — when those expire, France/EU ad pages stop working first (DSA transparency rules).

**Fix:**
1. Log into Facebook in your browser with the account used for the bot
2. Export fresh cookies and update the `FACEBOOK_COOKIES` secret
3. Run: `python telegram-bot/fb_login.py`
4. Restart the **Telegram Bot** workflow

The script injects the new cookies, verifies the session, and saves the auth state. You only need to redo this when the session expires again.

## TypeScript Packages

### `artifacts/api-server` (`@workspace/api-server`)
Express 5 API server. Routes at `src/routes/`.

### `lib/db` (`@workspace/db`)
Drizzle ORM + PostgreSQL. Push schema: `pnpm --filter @workspace/db run push`

### `lib/api-spec` (`@workspace/api-spec`)
OpenAPI spec + codegen: `pnpm --filter @workspace/api-spec run codegen`
