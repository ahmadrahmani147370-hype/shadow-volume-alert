# SHADOW VOLUME ALERT

A Telegram bot that scans trending **Solana meme coin pools** every 5 minutes and alerts you when a pool's
**5-minute volume suddenly jumps** compared with the previous 5-minute reading.

* Runs on your Android phone in **Termux**. No VPS, no website, no database.
* Uses only **free public APIs** with no keys: **DexScreener** and **GeckoTerminal**.
* Saves its history in a small local file (`state.json`), so restarting the bot keeps the comparison data.

---

## 1. Alert rules (default)

An alert is sent only when ALL of these are true for a pool:

| Rule | Default |
|---|---|
| 5-minute volume | ≥ $10,000 |
| Liquidity | ≥ $10,000 |
| 5-minute transactions (buys + sells) | ≥ 20 |
| Current 5m volume vs previous 5m reading | ≥ 2.5x |
| Previous reading age | 4 to 7 minutes (older readings are never compared) |
| Same pool / same coin | max 1 alert per 60 minutes |

**How the bot avoids fake signals:**
1. All numbers come from live DexScreener data, nothing is estimated or invented.
2. Before alerting, the bot **re-downloads the pool** and checks the rules again.
3. Then it **cross-checks with GeckoTerminal** (an independent data source). If GeckoTerminal does not
   confirm real volume, liquidity and trades, no alert is sent.
4. If the previous reading is too old (phone slept, bot was off), the bot only stores a new baseline.
5. SOL, stablecoins, wrapped/bridged majors (BTC, ETH, ZEC...), liquid-staking tokens and tokenized stocks
   (e.g. "... Backpack Securities" xStocks) are ignored, so you only get meme coins. Edit `EXCLUDE_SYMBOLS`
   in `config.env` to change the list.

Each alert shows: coin name and symbol, 5m volume, previous volume, the multiplier, liquidity, buys, sells,
buy %, price, 5m price change, market cap, 1h volume, DEX, pool age, mint address, pair address and
DexScreener + GeckoTerminal chart links.

---

## 2. Create your Telegram bot (token)

1. Open Telegram and search for **@BotFather** (blue tick).
2. Send `/newbot`.
3. Give it a name, e.g. `Shadow Volume Alert`.
4. Give it a username ending in `bot`, e.g. `my_shadow_volume_bot`.
5. BotFather replies with a **token** like `1234567890:AAHk3x...`. Copy it. Keep it secret.

## 3. Get your Chat ID

**For alerts in your private chat (easiest):**
1. Open your new bot in Telegram and press **Start** (or send it any message, e.g. `hi`).
2. In a browser, open (replace `<TOKEN>` with your token, keep the word `bot` in front):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id":123456789,...`. That number is your **Chat ID**.
   If you see `"result":[]`, send the bot another message and refresh the page.

**For a group:** add the bot to the group, send a message in the group (e.g. `/start@your_bot_name`),
then open the same `getUpdates` link. Group IDs are negative, e.g. `-1001234567890`.
If you see nothing, open @BotFather → `/setprivacy` → choose your bot → **Disable**, then send a new message.

**For a channel:** add the bot to the channel as an **administrator** (allowed to post messages), post a
message in the channel, then open `getUpdates` and look for `"channel_post"` → `"chat":{"id":-100...}`.
You can also use `@yourchannelusername` as the Chat ID for public channels.

---

## 4. Install on Android (Termux)

> Install Termux from **F-Droid** (https://f-droid.org/packages/com.termux/) or GitHub.
> The Google Play version is outdated and often breaks `pkg`.
> For the wake-lock to work fully, also install **Termux:API** from the same place (optional).

Open Termux and run these commands one by one:

```bash
pkg update -y && pkg upgrade -y
pkg install -y python unzip nano
```

Optional, lets Termux read your phone's Download folder:

```bash
termux-setup-storage
```
(press **Allow** on the pop-up)

### Get the bot files onto the phone

**Option A – the zip file (recommended):** download `shadow_volume_alert.zip` to your phone's
Download folder (from Telegram, Drive, email...), then:

```bash
cp ~/storage/downloads/shadow_volume_alert.zip ~/
cd ~
unzip -o shadow_volume_alert.zip
cd shadow_volume_alert
```

**Option B – paste the files by hand:**

```bash
mkdir -p ~/shadow_volume_alert && cd ~/shadow_volume_alert
nano shadow_volume_alert.py     # paste the code, then Ctrl+O, Enter, Ctrl+X to save
nano requirements.txt           # paste:  requests>=2.28
nano start.sh                   # paste start.sh
nano config.example.env         # paste config.example.env
```

(If you host the files somewhere with a direct link, you can also use
`curl -L -o shadow_volume_alert.zip "DIRECT_LINK"` instead.)

### Install the Python library

```bash
cd ~/shadow_volume_alert
pip install -r requirements.txt
```

Only `requests` is needed; it installs instantly, nothing needs compiling.

### Add your token and chat ID

```bash
cp config.example.env config.env
nano config.env
```

Replace `PUT_YOUR_BOT_TOKEN_HERE` and `PUT_YOUR_CHAT_ID_HERE` with your values, for example:

```
TELEGRAM_BOT_TOKEN=1234567890:AAHk3xYourRealTokenHere
TELEGRAM_CHAT_ID=123456789
```

Save: **Ctrl+O**, **Enter**, **Ctrl+X**. (In Termux the Ctrl key is on the extra keys row.)

### Test it

```bash
python shadow_volume_alert.py --selftest        # offline logic test, should say SELFTEST PASSED
python shadow_volume_alert.py --test-telegram   # you should get a test message in Telegram
python shadow_volume_alert.py --once            # one real scan, shows what it found
```

---

## 5. Run it 24/7

```bash
cd ~/shadow_volume_alert
bash start.sh
```

`start.sh` keeps the phone CPU awake (`termux-wake-lock`) and **restarts the bot automatically** if it crashes
(30 seconds later). If there is no internet at start, the bot waits and retries by itself. Only a wrong bot token
stops the loop, with a message telling you to fix `config.env`.
You get a "SHADOW VOLUME ALERT is online" message in Telegram. The first scan only records a baseline, so the
earliest alerts can come after the second scan (about 5 minutes later).

### Keep it running after you close Termux

Run it in the background:

```bash
cd ~/shadow_volume_alert
nohup bash start.sh > bot_output.txt 2>&1 &
```

Or with tmux (lets you look at the live screen again later):

```bash
pkg install -y tmux
tmux new -s shadow          # opens a session
bash start.sh               # start the bot inside it
# press Ctrl+B then D to detach. The bot keeps running.
tmux attach -t shadow       # come back later to watch it
```

Watch the log: `tail -f ~/shadow_volume_alert/shadow_volume_alert.log` (Ctrl+C stops watching, not the bot).

Stop the bot: `bash ~/shadow_volume_alert/stop.sh` (or Ctrl+C if it is running in front of you)

### Example of what you will see in the log

```
[INFO] ----- Scan #2 -----
[INFO] Discovery: 89 GeckoTerminal pools, 46 DexScreener promoted tokens -> 21 pairs, 108 watchlist -> 146 unique pools
[INFO] Near misses: RUDY 1.82x (increase 1.82x < 2.50x)
[INFO] ALERT sent: DCTY 2.91x 5m vol $13.4K
[INFO] Scan done in 34s: 145 pools read, 31 pass volume+liquidity, 12 new baselines, 1 alerts.
```

`Rate limited by api.geckoterminal.com, waiting 10s` warnings are normal; the bot waits and retries.

### IMPORTANT: stop Android from killing Termux

1. Pull down the notification bar → Termux notification → tap **Acquire wakelock**
   (`start.sh` also does this automatically).
2. Android **Settings → Apps → Termux → Battery → Unrestricted** (or "Don't optimize").
   On Xiaomi/Redmi/POCO also enable **Autostart**; on Samsung remove Termux from "Sleeping apps";
   on Huawei/Honor set **Launch → Manage manually** and enable all switches.
3. Keep the phone charging and on Wi-Fi/data if you run it all day.
4. Do not swipe Termux away from the recent apps list.

---

## 6. Settings

All settings are in `config.env` (see the comments in `config.example.env`). After changing anything,
stop and start the bot again. Examples:

* More alerts: `VOLUME_MULTIPLIER=2` or `MIN_VOLUME_5M_USD=5000`
* Fewer alerts: `VOLUME_MULTIPLIER=3` or `ALERT_COOLDOWN_MINUTES=120`

---

## 7. Files

| File | What it is |
|---|---|
| `shadow_volume_alert.py` | the bot |
| `config.example.env` | settings template; copy it to `config.env` |
| `config.env` | your settings + token (created by you, keep private) |
| `requirements.txt` | Python library list (`requests`) |
| `start.sh` / `stop.sh` | start with auto-restart / stop |
| `state.json` | saved volume readings, alert history, watchlist (auto-created) |
| `shadow_volume_alert.log` | log file (auto-rotated, max ~4 MB) |

Delete `state.json` only if you want to reset all history.

---

## 8. How it works (technical)

Every 5 minutes:

1. **Discover pools**
   * GeckoTerminal `GET /api/v2/networks/solana/trending_pools?duration=5m` (pages 1-3),
     `trending_pools?duration=1h`, and `new_pools`.
   * DexScreener `GET /token-boosts/latest/v1`, `/token-boosts/top/v1`, `/token-profiles/latest/v1`
     (Solana tokens only), then `GET /tokens/v1/solana/{up to 30 mints}` to pick each token's most liquid pool.
   * Pools seen in the last 3 hours stay on a watchlist, so a coin that drops off trending is still compared.
2. **Read all pools from one source**: DexScreener `GET /latest/dex/pairs/solana/{up to 30 pairs}`
   (5m volume, 5m buys/sells, liquidity, price, market cap).
3. **Compare** each pool's 5m volume with its stored reading from 4-7 minutes earlier.
4. **Verify** hits (fresh DexScreener re-read + GeckoTerminal `GET /pools/multi/{pool}`), check the cooldown,
   then send with Telegram `sendMessage` (HTML).
5. **Save** readings to `state.json` with an atomic write (temp file + rename), so a crash or power loss
   cannot corrupt it. Old entries are pruned automatically.

Rate limits: DexScreener allows ~60 req/min (boost/profile endpoints) and ~300 req/min (pair endpoints);
GeckoTerminal's free API allows ~30 req/min. The bot uses roughly 10-25 requests per scan, spaces its calls,
and backs off automatically on HTTP 429 / errors / timeouts. Telegram flood limits (`retry_after`) are respected.

### Things to know

* DexScreener's "5m volume" is a **rolling** last-5-minutes window, and APIs cache data for up to ~1 minute,
  so readings are close to, not exactly, aligned 5-minute candles.
* Real 2.5x spikes on pools that already trade $10K per 5 minutes are rare. Quiet hours can pass with no alert;
  that is normal and intended (no fake signals).
* If the phone sleeps or the internet drops for more than 7 minutes, the next scan just rebuilds the baseline.
* This is a data tool, not financial advice. Meme coins are extremely risky.
