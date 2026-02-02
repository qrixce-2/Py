import asyncio
import random
import uuid
import os
import time
import json
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, RateLimitError
import threading
from flask import Flask

# ================= ENVIRONMENT VARIABLES =================

USERNAME = os.environ.get('IG_USERNAME')
PASSWORD = os.environ.get('IG_PASSWORD')

if not USERNAME or not PASSWORD:
    print("❌ ERROR: IG_USERNAME and IG_PASSWORD must be set as environment variables")
    print("On Render: Go to Environment → Add IG_USERNAME and IG_PASSWORD")
    print("On Local: export IG_USERNAME=your_username")
    print("          export IG_PASSWORD=your_password")
    exit(1)

print(f"✅ Logging in as: {USERNAME}")

# ================= CONFIG =================

SESSION_FILE = "session.json"

MSG_DELAY = 30
TITLE_DELAY = 10
ERROR_COOLDOWN = 120
TITLE_DELAY_BETWEEN_GCS = 200

MESSAGE_FILE = "message.txt"
TITLE_FILE = "nc.txt"

DOC_ID = "29088580780787855"
IG_APP_ID = "936619743392459"

# ================= LOAD TEXT FILES =================

def load_lines(file_path):
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        print("Creating empty file...")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("Hello everyone!\nHow are you?\nGood morning!")
        return ["Default message 1", "Default message 2"]
    
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

MESSAGES = load_lines(MESSAGE_FILE)
TITLES = load_lines(TITLE_FILE)

print(f"✅ Loaded {len(MESSAGES)} messages and {len(TITLES)} titles")

# ================= COLOR LOGGING =================

GREEN = "\033[92m"
CYAN = "\033[96m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

def log_message(username, gid):
    print(f"{GREEN}({username}) sent message to {gid}{RESET}")

def log_title(username, title):
    print(f"{CYAN}({username}) changed group name to \"{title}\"{RESET}")

def log_error(msg):
    print(f"{RED}{msg}{RESET}")

def log_warn(msg):
    print(f"{YELLOW}{msg}{RESET}")

# ================= CLIENT SETUP =================

cl = Client()
login_lock = asyncio.Lock()

def setup_mobile_fingerprint():
    cl.set_user_agent(
        "Instagram 312.0.0.22.114 Android "
        "(33/13; 420dpi; 1080x2400; OnePlus; "
        "GM1913; OnePlus7Pro; qcom; en_US)"
    )

    cl.set_locale("en_US")
    cl.set_country_code(1)
    cl.set_timezone_offset(-18000)

    uuids = {
        "phone_id": str(uuid.uuid4()),
        "uuid": str(uuid.uuid4()),
        "client_session_id": str(uuid.uuid4()),
        "advertising_id": str(uuid.uuid4()),
        "device_id": "android-" + uuid.uuid4().hex[:16]
    }
    cl.set_uuids(uuids)

    cl.private.headers.update({
        "X-IG-App-ID": IG_APP_ID,
        "X-IG-Device-ID": uuids["uuid"],
        "X-IG-Android-ID": uuids["device_id"],
        "X-IG-App-Locale": "en_US",
        "X-IG-Device-Locale": "en_US",
        "X-IG-Mapped-Locale": "en_US",
        "X-IG-Timezone-Offset": str(-18000),
        "Accept-Language": "en-US",
        "Connection": "keep-alive"
    })

# ================= LOGIN FLOW =================

async def login():
    async with login_lock:
        if os.path.exists(SESSION_FILE):
            try:
                cl.load_settings(SESSION_FILE)
                cl.login(USERNAME, PASSWORD)
                cl.get_timeline_feed()
                print(f"{GREEN}✅ Logged in using saved session{RESET}")
                return
            except Exception as e:
                log_warn(f"⚠️ Saved session expired — re-logging... ({e})")

        try:
            cl.login(USERNAME, PASSWORD)
            cl.dump_settings(SESSION_FILE)
            print(f"{GREEN}✅ Logged in with username & password{RESET}")
        except Exception as e:
            log_error(f"❌ Login failed: {e}")
            raise

# ================= FETCH GROUP THREADS =================

def fetch_group_threads():
    print("📡 Fetching group threads...")
    try:
        threads = cl.direct_threads(amount=100)
        group_ids = [t.id for t in threads if getattr(t, "is_group", False)]
        print(f"✅ Found {len(group_ids)} group threads")
        return group_ids
    except Exception as e:
        log_error(f"❌ Failed to fetch threads: {e}")
        return []

# ================= ASYNC WRAPPER =================

async def api_call(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# ================= TITLE CHANGE LOGIC =================

def graphql_rename(thread_id, title):
    try:
        csrf = cl.private.cookies.get("csrftoken", "")
        cl.private.headers.update({
            "User-Agent": cl.user_agent,
            "X-CSRFToken": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/direct/t/{thread_id}/",
        })

        payload = {
            "doc_id": DOC_ID,
            "variables": json.dumps({
                "thread_fbid": str(thread_id),
                "new_title": title
            })
        }

        r = cl.private.post(
            "https://www.instagram.com/api/graphql/",
            data=payload,
            timeout=10
        )

        return r.status_code == 200
    except Exception as e:
        log_error(f"❌ GraphQL rename failed: {e}")
        return False

def rename_thread(thread_id, title):
    try:
        cl.private_request(
            f"direct_v2/threads/{thread_id}/update_title/",
            data={"title": title}
        )
        return True

    except RateLimitError:
        log_warn(f"⚠️ Rate limited on API rename {thread_id} → trying GraphQL...")
        return graphql_rename(thread_id, title)

    except Exception as e:
        msg = str(e).lower()
        if "rate" in msg or "429" in msg or "blocked" in msg:
            log_warn(f"⚠️ API blocked {thread_id} → trying GraphQL...")
            return graphql_rename(thread_id, title)
        else:
            log_error(f"❌ Rename failed {thread_id}: {e}")
            return False

# ================= MESSAGE WORKER =================

async def message_worker(group_ids):
    print("📨 Message worker started")

    while True:
        try:
            if not group_ids:
                log_warn("⚠️ No group threads found. Checking again in 60 seconds...")
                await asyncio.sleep(60)
                group_ids = fetch_group_threads()
                continue

            for gid in group_ids:
                msg = random.choice(MESSAGES)
                await api_call(cl.direct_send, msg, thread_ids=[gid])
                log_message(USERNAME, gid)
                await asyncio.sleep(MSG_DELAY)

        except LoginRequired:
            log_warn("🔐 Session expired (message worker) — re-logging...")
            await login()
        except Exception as e:
            log_error(f"[MSG ERROR] {e}")
            await asyncio.sleep(ERROR_COOLDOWN)

# ================= TITLE WORKER =================

async def title_worker(group_ids):
    print("📝 Title worker started")

    while True:
        try:
            if not group_ids:
                log_warn("⚠️ No group threads found. Checking again in 60 seconds...")
                await asyncio.sleep(60)
                group_ids = fetch_group_threads()
                continue

            for gid in group_ids:
                title = random.choice(TITLES)
                success = await api_call(rename_thread, gid, title)

                if success:
                    log_title(USERNAME, title)
                else:
                    log_error(f"[TITLE FAILED] {gid}")

                await asyncio.sleep(TITLE_DELAY_BETWEEN_GCS)

            print(f"🔁 Title cycle done → sleep {TITLE_DELAY}")
            await asyncio.sleep(TITLE_DELAY)

        except LoginRequired:
            log_warn("🔐 Session expired (title worker) — re-logging...")
            await login()
        except Exception as e:
            log_error(f"[TITLE ERROR] {e}")
            await asyncio.sleep(ERROR_COOLDOWN)

# ================= FLASK WEB SERVER =================

def run_flask():
    app = Flask(__name__)
    
    @app.route('/')
    def home():
        return """
        <h1>Instagram Bot is Running! ✅</h1>
        <p>Bot is active and working in the background.</p>
        <p>Username: """ + USERNAME + """</p>
        <p>Status: <span style="color: green;">Online</span></p>
        <p>Check console/logs for detailed activity.</p>
        """
    
    @app.route('/health')
    def health():
        return {"status": "healthy", "bot": "running", "timestamp": time.time()}
    
    @app.route('/status')
    def status():
        return {
            "username": USERNAME,
            "messages_loaded": len(MESSAGES),
            "titles_loaded": len(TITLES),
            "service": "instagram-group-bot",
            "uptime": time.time() - start_time
        }
    
    port = int(os.environ.get("PORT", 8080))
    print(f"🌐 Web server starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ================= MAIN =================

async def main():
    print("🤖 Instagram Bot Starting...")
    print("=" * 50)
    
    setup_mobile_fingerprint()
    await login()
    group_ids = fetch_group_threads()
    
    if not group_ids:
        log_warn("⚠️ No group threads found. Bot will continue checking...")
    
    print("✅ Bot initialization complete!")
    print("📱 Message worker: Active")
    print("🏷️  Title worker: Active")
    print("🌐 Web server: Active")
    print("=" * 50)
    
    # Run both workers
    await asyncio.gather(
        message_worker(group_ids),
        title_worker(group_ids)
    )

# ================= START EVERYTHING =================

if __name__ == "__main__":
    start_time = time.time()
    
    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Give Flask time to start
    time.sleep(2)
    
    # Run the main bot
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        log_error(f"❌ Bot crashed: {e}")
        print("🔄 Restarting in 30 seconds...")
        time.sleep(30)
        # Restart
        os.execv(sys.executable, [sys.executable] + sys.argv)
