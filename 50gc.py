import asyncio
import random
import os
import time
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, RateLimitError

# ================= CONFIG =================

SESSION_FILE = "session.json"

MSG_DELAY = 30
TITLE_DELAY_BETWEEN_GCS = 200
CYCLE_DELAY = 60
ERROR_COOLDOWN = 120

MESSAGE_FILE = "message.txt"
TITLE_FILE = "nc.txt"

IG_APP_ID = "936619743392459"

# ================= LOAD TEXT FILES =================

def load_lines(file_path):
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        exit()
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

MESSAGES = load_lines(MESSAGE_FILE)
TITLES = load_lines(TITLE_FILE)
TITLES_SET = set(TITLES)

# ================= CLIENT =================

cl = Client()

def setup_client():
    cl.private.headers.update({
        "X-IG-App-ID": IG_APP_ID,
        "Accept-Language": "en-US",
        "Connection": "keep-alive"
    })

# ================= LOGIN =================

def login_with_session():
    cl.load_settings(SESSION_FILE)
    cl.get_timeline_feed()
    print("✅ Logged in using session.json")

# ================= TITLE SAFE GET =================

def get_thread_title(thread):
    return (
        getattr(thread, "thread_title", None)
        or getattr(thread, "thread_name", None)
        or ""
    ).strip()

# ================= FETCH GROUPS (METHOD 1 + 2) =================

def fetch_group_threads():
    collected = {}

    # ---- METHOD 1: normal fetch ----
    threads = cl.direct_threads(amount=100)
    for t in threads:
        if getattr(t, "is_group", False):
            collected[t.id] = get_thread_title(t)

    # ---- METHOD 2: refresh inbox if few groups ----
    if len(collected) < 10:
        print("🔄 Inbox refresh fetch triggered")
        time.sleep(5)

        threads = cl.direct_threads(amount=100)
        for t in threads:
            if getattr(t, "is_group", False):
                collected[t.id] = get_thread_title(t)

    return list(collected.items())

# ================= ASYNC HELPER =================

async def api_call(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# ================= SEQUENTIAL WORKER =================

async def sequential_worker():
    print("🔁 Sequential group worker started")

    while True:
        try:
            groups = fetch_group_threads()
            print(f"📋 Processing {len(groups)} groups")

            for gid, current_title in groups:
                print(f"\n➡️ Group: {gid}")

                # ---- SEND MESSAGE ----
                try:
                    await api_call(
                        cl.direct_send,
                        random.choice(MESSAGES),
                        thread_ids=[gid]
                    )
                    print("   📩 Message sent")
                except Exception as e:
                    print(f"   ⛔ Message error")

                await asyncio.sleep(MSG_DELAY)

                # ---- RENAME CHECK ----
                if current_title and current_title in TITLES_SET:
                    print(f"   ⏭ Rename skipped")
                    continue

                # ---- RENAME ----
                try:
                    cl.private_request(
                        f"direct_v2/threads/{gid}/update_title/",
                        data={"title": random.choice(TITLES)}
                    )
                    print("   ✏️ Group renamed")
                except Exception:
                    print("   ⛔ Rename error")

                await asyncio.sleep(TITLE_DELAY_BETWEEN_GCS)

            print("\n✅ Cycle complete — sleeping")
            await asyncio.sleep(CYCLE_DELAY)

        except LoginRequired:
            print("❌ Session expired")
            raise SystemExit
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            await asyncio.sleep(ERROR_COOLDOWN)

# ================= MAIN =================

async def main():
    setup_client()
    login_with_session()
    await sequential_worker()

asyncio.run(main())