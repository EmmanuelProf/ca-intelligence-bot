"""
CA Intelligence Bot — Telegram Channel Listener
Monitors public Telegram channels for contract addresses (BSC/SOL/Base/Tron)
and forwards them to n8n for analysis.

Auth modes:
  - BOT mode:  add bot as admin to channels you own (simpler, limited)
  - USER mode: uses your personal account (can monitor any public channel)
               Set TELEGRAM_PHONE in env to enable user mode.
               On first run locally, it will ask for OTP — saves session string
               to console. Copy that string into TELEGRAM_SESSION env var on Render.
"""

import os
import re
import logging
import asyncio
import aiohttp
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
API_ID        = int(os.environ["TELEGRAM_API_ID"])
API_HASH      = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PHONE_NUMBER  = os.environ.get("TELEGRAM_PHONE", "")       # e.g. +2348012345678
SESSION_STR   = os.environ.get("TELEGRAM_SESSION", "")     # saved StringSession
N8N_WEBHOOK   = os.environ["N8N_WEBHOOK_URL"]              # /webhook/ca-intel URL

CHANNELS = [
    c.strip().lstrip("@")
    for c in os.environ.get("WATCH_CHANNELS", "").split(",")
    if c.strip()
]

# ── CA Pattern Detection ──────────────────────────────────────────────────────
EVM_PATTERN = re.compile(r'\b(0x[a-fA-F0-9]{40})\b')
TRX_PATTERN = re.compile(r'\b(T[1-9A-HJ-NP-Za-km-z]{33})\b')
# Solana: base58, almost always exactly 44 chars — raise floor to cut false positives
SOL_PATTERN = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{43,44})\b')

# Known non-token Solana addresses to ignore
SOL_NOISE = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", # USDT
    "11111111111111111111111111111111",               # System program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Token program
}

def extract_cas(text: str) -> list[dict]:
    """Extract contract addresses from message text. EVM takes priority over SOL."""
    found = []
    seen  = set()

    # EVM first (most specific pattern)
    for match in EVM_PATTERN.finditer(text):
        addr = match.group(1).lower()
        if addr not in seen:
            seen.add(addr)
            found.append({"address": addr, "chain": "evm"})

    # Tron
    for match in TRX_PATTERN.finditer(text):
        addr = match.group(1)
        if addr not in seen:
            seen.add(addr)
            found.append({"address": addr, "chain": "tron"})

    # Solana — only attempt if no EVM/Tron found (reduces false positives)
    if not found:
        for match in SOL_PATTERN.finditer(text):
            addr = match.group(1)
            if addr not in seen and addr not in SOL_NOISE:
                seen.add(addr)
                found.append({"address": addr, "chain": "solana"})

    return found

# ── Webhook Sender ────────────────────────────────────────────────────────────
async def send_to_n8n(session: aiohttp.ClientSession, payload: dict):
    try:
        async with session.post(
            N8N_WEBHOOK,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                log.info(f"✅ Forwarded to n8n: {payload['address']} ({payload['chain']})")
            else:
                body = await resp.text()
                log.warning(f"⚠️  n8n {resp.status} for {payload['address']}: {body[:100]}")
    except asyncio.TimeoutError:
        log.error(f"❌ Timeout sending {payload['address']} to n8n")
    except Exception as e:
        log.error(f"❌ Failed to send to n8n: {e}")

# ── Client Factory ────────────────────────────────────────────────────────────
def build_client() -> TelegramClient:
    """
    Build the right client based on available credentials.
    Priority: SESSION_STR (user) > PHONE_NUMBER (user, interactive) > BOT_TOKEN (bot)
    """
    if SESSION_STR:
        log.info("🔑 Auth mode: USER (StringSession from env)")
        return TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)

    if PHONE_NUMBER:
        log.info("🔑 Auth mode: USER (phone — interactive OTP on first run)")
        return TelegramClient(StringSession(), API_ID, API_HASH)

    if BOT_TOKEN:
        log.info("🔑 Auth mode: BOT TOKEN — bot must be admin in monitored channels")
        return TelegramClient(StringSession(), API_ID, API_HASH)

    raise RuntimeError(
        "No auth credentials found. Set TELEGRAM_SESSION, TELEGRAM_PHONE, or TELEGRAM_BOT_TOKEN."
    )

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    if not CHANNELS:
        log.error("No channels configured. Set WATCH_CHANNELS env var (comma-separated).")
        return

    log.info(f"Channels to monitor: {CHANNELS}")

    client = build_client()

    # Start with the right credentials
    if SESSION_STR:
        await client.start()
    elif PHONE_NUMBER:
        await client.start(phone=PHONE_NUMBER)
        # On first run — print session string so you can save it to env
        session_string = client.session.save()
        log.info("=" * 60)
        log.info("SAVE THIS SESSION STRING TO TELEGRAM_SESSION env var:")
        log.info(session_string)
        log.info("=" * 60)
    else:
        await client.start(bot_token=BOT_TOKEN)

    me = await client.get_me()
    log.info(f"Logged in as: {getattr(me, 'username', None) or getattr(me, 'first_name', 'unknown')}")

    # Resolve channel entities
    channel_entities = []
    for username in CHANNELS:
        try:
            entity = await client.get_entity(username)
            channel_entities.append(entity)
            log.info(f"✅ Monitoring: @{username}")
        except Exception as e:
            log.warning(f"⚠️  Could not resolve @{username}: {e}")

    if not channel_entities:
        log.error("No valid channels resolved. Check WATCH_CHANNELS and that the account can access them.")
        return

    async with aiohttp.ClientSession() as http:

        @client.on(events.NewMessage())
        async def handler(event):
            msg  = event.message
            text = msg.message or ""

            # Debug: log every message received
            try:
                chat = await event.get_chat()
                chat_name = getattr(chat, "username", None) or getattr(chat, "title", None) or str(chat.id)
                log.info(f"📨 Message in @{chat_name}: {text[:80]!r}")
            except Exception as e:
                log.info(f"📨 Message received (chat unknown): {text[:80]!r}")

            if not text:
                return

            cas = extract_cas(text)
            if not cas:
                return

            # Sender info
            sender_name = "unknown"
            try:
                sender = await event.get_sender()
                if sender:
                    sender_name = (
                        getattr(sender, "username", None) or
                        getattr(sender, "first_name", None) or
                        "unknown"
                    )
            except Exception:
                pass

            # Channel name
            chat = await event.get_chat()
            channel_name = (
                getattr(chat, "username", None) or
                getattr(chat, "title", None) or
                "unknown"
            )

            for ca in cas:
                payload = {
                    "address":      ca["address"],
                    "chain":        ca["chain"],
                    "raw_message":  text[:500],
                    "sender":       sender_name,
                    "channel":      channel_name,
                    "timestamp":    datetime.utcnow().isoformat(),
                    "message_link": f"https://t.me/{channel_name}/{msg.id}"
                }
                log.info(f"🔎 CA in @{channel_name} from {sender_name}: {ca['address']} [{ca['chain']}]")
                await send_to_n8n(http, payload)

        log.info("👂 Listening for messages... (Ctrl+C to stop)")
        await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
