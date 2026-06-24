# Waifu Name Lookup Bot

A Telegram bot that identifies anime characters and scenes from images using SauceNAO and trace.moe APIs.

## Features

- Character identification via SauceNAO (100 requests/day)
- Scene recognition via trace.moe (rate limited)
- Redis caching for fast repeat lookups (0.01s)
- MongoDB for permanent storage (0.05s)
- User statistics tracking
- Multi-image support (JPG, PNG, WEBP)
- Admin commands

## Requirements

- Python 3.8+
- Redis (port 6379)
- MongoDB (port 27017)
- FFmpeg

## Installation

```bash
git clone https://github.com/your-username/waifu-name-lookup-bot.git
cd waifu-name-lookup-bot
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

## Usage

### Commands
- `/start` - Welcome message
- `/waifu` or `/w` or `/name` - Show help
- `/stats` - Show bot statistics
- `/clearcache` - Clear cache (admin only)

### Image Lookup
Send an anime image and the bot will identify the character or scene.

## Credentials Needed

1. **BOT_TOKEN** from @BotFather
2. **API_ID** and **API_HASH** from my.telegram.org
3. **SAUCENAO_KEY** from saucenao.com
4. **ADMIN_IDS** your Telegram user ID

## License

MIT
