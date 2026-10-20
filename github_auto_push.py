import asyncio
import base64
import json
import os
import time
import requests
from pathlib import Path
from playwright.async_api import async_playwright

BASE_DIR = r"D:\Flow-AI-Video-Studio"
STATUS_FILE = os.path.join(BASE_DIR, "github_push_status.txt")
GH_PROFILE_DIR = os.path.join(BASE_DIR, "profiles", "github_auth_profile")
os.makedirs(GH_PROFILE_DIR, exist_ok=True)

FILES_TO_PUSH = [
    "app.py",
    "flow_engine.py",
    "post_processor.py",
    "database.py",
    "desktop_escape.py",
    "github_auto_push.py",
    "CHAY_TOOL_FLOW_AI.bat",
    "requirements.txt",
    "README.md",
    ".gitignore",
    "audio_library/bgm/warm_acoustic_lofi.wav",
    "audio_library/bgm/bright_commercial_upbeat.wav",
    "audio_library/bgm/cinematic_ambient_nature.wav",
    "audio_library/sfx/whoosh_transition.wav",
    "audio_library/sfx/pop_reveal.wav",
    "audio_library/sfx/sparkle_chime.wav",
    "audio_library/sfx/cinematic_impact.wav",
]


def log_status(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    with open(STATUS_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def push_files_to_github(access_token: str, repo_name: str = "Flow-AI-Video-Studio") -> str:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    user_r = requests.get("https://api.github.com/user", headers=headers, timeout=15)
    user_r.raise_for_status()
    owner = user_r.json()["login"]
    log_status(f"✅ Đã xác thực tài khoản GitHub: @{owner}")

    # Check if repo exists; if not, create it
    repo_check = requests.get(f"https://api.github.com/repos/{owner}/{repo_name}", headers=headers, timeout=15)
    if repo_check.status_code == 404:
        log_status(f"📦 Đang khởi tạo Repository mới: {owner}/{repo_name}...")
        create_r = requests.post(
            "https://api.github.com/user/repos",
            headers=headers,
            json={
                "name": repo_name,
                "description": "Flow AI Video Studio - Multi-Account & Multi-IP 8s Video Automation + FFmpeg Post-Production & Antigravity 2.0",
                "private": False,
                "auto_init": True,
            },
            timeout=20,
        )
        create_r.raise_for_status()
        time.sleep(2.0)
    else:
        log_status(f"📦 Đã tìm thấy Repository: {owner}/{repo_name}")

    # Upload each file via Contents API
    for rel_path in FILES_TO_PUSH:
        full_p = os.path.join(BASE_DIR, rel_path.replace("/", os.sep))
        if not os.path.exists(full_p):
            continue
        with open(full_p, "rb") as f:
            b64_content = base64.b64encode(f.read()).decode("ascii")

        api_url = f"https://api.github.com/repos/{owner}/{repo_name}/contents/{rel_path}"
        existing = requests.get(api_url, headers=headers, timeout=15)
        payload = {
            "message": f"Update {rel_path} - Flow AI Video Studio",
            "content": b64_content,
        }
        if existing.status_code == 200:
            payload["sha"] = existing.json().get("sha")

        put_r = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if put_r.status_code in (200, 201):
            log_status(f"⬆️ Đã đẩy lên GitHub: {rel_path}")
        else:
            log_status(f"⚠️ Lỗi đẩy {rel_path}: HTTP {put_r.status_code} {put_r.text[:120]}")

    repo_url = f"https://github.com/{owner}/{repo_name}"
    log_status(f"🎉 HOÀN TẤT ĐẨY CODE LÊN GITHUB: {repo_url}")
    return repo_url


async def main() -> None:
    if os.path.exists(STATUS_FILE):
        os.remove(STATUS_FILE)

    # 1. Request GitHub OAuth Device Code
    r = requests.post(
        "https://github.com/login/device/code",
        headers={"Accept": "application/json"},
        data={"client_id": "178c6fc778ccc68e1d6a", "scope": "repo workflow"},
        timeout=15,
    )
    r.raise_for_status()
    dev_data = r.json()
    device_code = dev_data["device_code"]
    user_code = dev_data["user_code"]
    interval = max(5, int(dev_data.get("interval", 5)))

    log_status(f"🔑 Mã xác thực GitHub (Tự động điền sau khi đăng nhập): {user_code}")

    async with async_playwright() as p:
        launch_kwargs = {
            "user_data_dir": GH_PROFILE_DIR,
            "headless": False,
            "no_viewport": True,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1150,820",
                "--window-position=120,60",
            ],
        }
        try:
            ctx = await p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        except Exception:
            ctx = await p.chromium.launch_persistent_context(channel="msedge", **launch_kwargs)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.bring_to_front()
        await page.goto("https://github.com/login?return_to=%2Flogin%2Fdevice", wait_until="domcontentloaded")
        log_status("🌐 Đã mở trình duyệt GitHub! Đang đợi anh đăng nhập tài khoản GitHub...")

        access_token = ""
        code_filled = False
        last_poll = 0.0
        deadline = time.monotonic() + 850

        while time.monotonic() < deadline and len(ctx.pages) > 0:
            curr_url = page.url or ""

            # When user reaches /login/device after logging in, auto-fill the user_code!
            if "/login/device" in curr_url and not code_filled:
                try:
                    # GitHub device page has either 1 input or 8 character inputs (#user-code-0 .. #user-code-7)
                    filled_ok = await page.evaluate(
                        """(code) => {
                            const clean = code.replace('-', '');
                            const charInputs = [...document.querySelectorAll('input[id^="user-code-"], input.js-user-code-input')];
                            if (charInputs.length >= 8) {
                                for (let i = 0; i < 8; i++) {
                                    charInputs[i].focus();
                                    charInputs[i].value = clean[i];
                                    charInputs[i].dispatchEvent(new Event('input', {bubbles: true}));
                                    charInputs[i].dispatchEvent(new Event('change', {bubbles: true}));
                                }
                                const btn = document.querySelector('button[type="submit"], input[type="submit"]');
                                if (btn) setTimeout(() => btn.click(), 400);
                                return true;
                            }
                            const single = document.querySelector('input#user_code, input[name="user_code"]');
                            if (single) {
                                single.value = code;
                                single.dispatchEvent(new Event('input', {bubbles: true}));
                                const btn = document.querySelector('button[type="submit"], input[type="submit"]');
                                if (btn) setTimeout(() => btn.click(), 400);
                                return true;
                            }
                            return false;
                        }""",
                        user_code,
                    )
                    if filled_ok:
                        code_filled = True
                        log_status(f"✨ Đã tự động điền mã {user_code} trên trang GitHub! Anh chỉ cần bấm nút màu xanh [Authorize GitHub CLI] (hoặc hệ thống sẽ tự bấm)...")
                except Exception:
                    pass

            # Also auto-click the green 'Authorize' button if it's enabled on the oauth authorize screen
            if "login/device/authorize" in curr_url or code_filled:
                try:
                    await page.evaluate(
                        """() => {
                            const btns = [...document.querySelectorAll('button[type="submit"], button.btn-primary')];
                            const authBtn = btns.find(b => !b.disabled && ((b.innerText || '').includes('Authorize') || (b.innerText || '').includes('Ủy quyền')));
                            if (authBtn) authBtn.click();
                        }"""
                    )
                except Exception:
                    pass

            # Poll GitHub OAuth token endpoint every `interval` seconds
            now = time.monotonic()
            if now - last_poll >= interval:
                last_poll = now
                try:
                    tok_r = requests.post(
                        "https://github.com/login/oauth/access_token",
                        headers={"Accept": "application/json"},
                        data={
                            "client_id": "178c6fc778ccc68e1d6a",
                            "device_code": device_code,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        },
                        timeout=10,
                    ).json()
                    if "access_token" in tok_r:
                        access_token = tok_r["access_token"]
                        break
                    elif tok_r.get("error") == "slow_down":
                        interval += 5
                except Exception:
                    pass

            await asyncio.sleep(1.2)

        if access_token:
            repo_url = push_files_to_github(access_token)
            try:
                await page.goto(repo_url, wait_until="domcontentloaded")
                log_status(f"🚀 Đã mở trang Repository GitHub của anh trên trình duyệt: {repo_url}")
                await asyncio.sleep(8.0)
            except Exception:
                pass
        else:
            log_status("❌ Chưa nhận được xác thực GitHub (Cửa sổ đã đóng hoặc hết thời gian chờ).")


if __name__ == "__main__":
    import sys
    from desktop_escape import get_current_desktop_name, spawn_on_default_desktop
    if os.name == "nt" and get_current_desktop_name().lower() != "default":
        cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
        spawn_on_default_desktop(cmd, work_dir=BASE_DIR, show_console=True)
        sys.exit(0)
    asyncio.run(main())
