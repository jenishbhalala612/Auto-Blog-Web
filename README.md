# 🚀 AutoBlog Engine

> Automated blog system: AI-powered content creation, SEO optimization, and self-healing deployment.

## Quick Start (Windows 11)

### Prerequisites

1. **Install Docker Desktop** (includes WSL2)
   - Download from https://www.docker.com/products/docker-desktop/
   - During install, enable WSL2 backend
   - Restart your PC after installation

2. **Install Git**
   - Download from https://git-scm.com/download/win
   - Use default settings during install

3. **Install Node.js 20 LTS**
   - Download from https://nodejs.org/
   - Choose the LTS version

4. **Install Python 3.11+**
   - Download from https://www.python.org/downloads/
   - ✅ Check "Add Python to PATH" during install

5. **Install Ollama**
   - Download from https://ollama.com/download
   - After install, pull a model:
   ```powershell
   ollama pull qwen2.5:14b
   ```

6. **Install Obsidian**
   - Download from https://obsidian.md/
   - Open the `vault/` folder as a vault

### Setup

```powershell
# 1. Clone or navigate to the project
cd d:\Jenish\Automation

# 2. Copy environment config
copy .env.example .env
# Edit .env with your settings (Telegram bot token, etc.)

# 3. Install website dependencies
cd website
npm install
cd ..

# 4. Install Python dependencies
cd pipeline
pip install -r requirements.txt
cd ..

# 5. Start all services with Docker
docker-compose up -d

# 6. Or run without Docker (development)
# Terminal 1: Website dev server
cd website && npm run dev

# Terminal 2: Pipeline
cd pipeline && python main.py
```

### Getting a Telegram Bot Token

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Follow the prompts to name your bot
4. Copy the token and paste it in `.env` as `TELEGRAM_BOT_TOKEN`
5. Send a message to your bot, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
6. Find your `chat_id` and paste it in `.env` as `TELEGRAM_CHAT_ID`

### Free Hosting Setup (Cloudflare Pages)

1. Push this repo to GitHub
2. Go to https://dash.cloudflare.com/ → Pages → Create a project
3. Connect your GitHub repo
4. Build settings:
   - Framework preset: Astro
   - Build command: `cd website && npm run build`
   - Build output directory: `website/dist`
5. Deploy — you'll get a free `*.pages.dev` domain

### Windows 11 — Running 24/7

```powershell
# Disable sleep
powercfg -change -standby-timeout-ac 0
powercfg -change -monitor-timeout-ac 30

# Set Windows Update active hours (Settings → Windows Update → Active hours)
# Set to 06:00 - 02:00 so it won't reboot during night jobs

# Auto-start Docker Desktop on login:
# Settings → General → Start Docker Desktop when you sign in
```

### Project Structure

```
├── website/          # Astro blog (English + Hindi)
├── pipeline/         # Python automation engine
├── vault/            # Obsidian content vault
├── openclaw/         # OpenClaw chat layer (Week 4)
├── docker-compose.yml
├── .env.example
└── README.md
```

### Commands

| Command | What it does |
|---------|-------------|
| `python pipeline/main.py` | Run full pipeline once |
| `python pipeline/main.py --schedule` | Start scheduler (runs daily) |
| `python pipeline/main.py --research` | Research only |
| `python pipeline/main.py --publish` | Publish approved posts only |
| `python pipeline/main.py --health` | Run health check |
| `python pipeline/main.py --summary` | Generate and send summary |
| `cd website && npm run dev` | Start website dev server |
| `cd website && npm run build` | Build for production |

### Troubleshooting

| Problem | Solution |
|---------|----------|
| Ollama not responding | Run `ollama serve` in a terminal |
| Build fails | Check `vault/📊 Logs/` for details |
| Telegram not sending | Verify bot token and chat_id in `.env` |
| Site down after deploy | Pipeline auto-rolls back; check Telegram |
| Docker won't start | Ensure WSL2 is enabled in BIOS (virtualization) |
