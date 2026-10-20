import asyncio
import base64
import json
import logging
import os
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests
from database import OUTPUTS_DIR, PROFILES_DIR, UPLOADS_DIR, compose_scene_prompt, get_conn

log = logging.getLogger("flow_engine")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

# Global live log buffer for Web UI polling
LIVE_LOGS: list[str] = []


class CreditExhaustedError(RuntimeError):
    """Raised when a Google Flow account has run out of AI credits so the worker can fail over to the next account."""
    pass


def add_log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("ascii", errors="replace"), flush=True)
    LIVE_LOGS.append(line)
    if len(LIVE_LOGS) > 300:
        del LIVE_LOGS[: len(LIVE_LOGS) - 300]


def parse_proxy(proxy_str: str) -> Optional[dict[str, str]]:
    """
    Support multiple proxy formats:
      1) http://user:pass@ip:port or socks5://user:pass@ip:port
      2) ip:port:user:pass
      3) ip:port
    Returns Playwright proxy dict: {'server': ..., 'username': ..., 'password': ...}
    """
    raw = (proxy_str or "").strip()
    if not raw:
        return None

    # Format ip:port:user:pass
    if "://" not in raw and raw.count(":") == 3:
        host, port, user, pw = raw.split(":", 3)
        return {
            "server": f"http://{host}:{port}",
            "username": user,
            "password": pw,
        }

    # Format ip:port
    if "://" not in raw and raw.count(":") == 1:
        return {"server": f"http://{raw}"}

    parsed = urlparse(raw)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or ""
    if not host or not port:
        return {"server": raw}

    res: dict[str, str] = {"server": f"{scheme}://{host}:{port}"}
    if parsed.username:
        res["username"] = parsed.username
    if parsed.password:
        res["password"] = parsed.password
    return res


def check_proxy_ip(proxy_str: str, timeout: int = 12) -> dict[str, Any]:
    """Verify the external IP of a proxy (or direct connection if empty)."""
    p = parse_proxy(proxy_str)
    proxies = None
    if p:
        srv = p["server"]
        if "username" in p and "password" in p:
            scheme, rest = srv.split("://", 1)
            proxy_auth_url = f"{scheme}://{p['username']}:{p['password']}@{rest}"
        else:
            proxy_auth_url = srv
        proxies = {"http": proxy_auth_url, "https": proxy_auth_url}

    try:
        r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=timeout)
        ip = r.json().get("ip", "")
        return {"ok": True, "ip": ip}
    except Exception as e:
        return {"ok": False, "ip": "", "error": str(e)}


def extract_reference_frames_from_video(video_path: str, max_frames: int = 2) -> list[str]:
    """
    If the user provides a reference VIDEO (.mp4, .mov, .webm), extract high-quality
    keyframes (.jpg) so Google Flow AI can attach them as Reference Ingredients or Start Frame.
    """
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in (".mp4", ".mov", ".webm", ".avi", ".mkv"):
        return [video_path] if os.path.exists(video_path) else []

    out_frames: list[str] = []
    base_name = Path(video_path).stem
    # Extract frame at 0.5s and 2.5s using ffmpeg
    timestamps = ["00:00:00.500", "00:00:02.000"][:max_frames]
    for idx, ts in enumerate(timestamps):
        out_jpg = os.path.join(UPLOADS_DIR, f"{base_name}_frame{idx+1}.jpg")
        if not os.path.exists(out_jpg):
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                ts,
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                out_jpg,
            ]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            except Exception as e:
                add_log(f"⚠️ Không thể tách frame từ video tham chiếu bằng ffmpeg: {e}")
        if os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 1000:
            out_frames.append(out_jpg)

    return out_frames


# ==============================================================================
# SELECTORS FROM TOP GITHUB REPOS (ffroliva/gflow-cli ⭐316 & eddie-fqh/flow-py)
# ==============================================================================
MODE_SWITCH_TRIGGER_SELECTORS = (
    "button[aria-haspopup='menu']:has(i.google-symbols:text('crop_16_9'))",
    "button[aria-haspopup='menu']:has(i.google-symbols:text('crop_9_16'))",
    "button[aria-haspopup='menu']:has(i.google-symbols:text('crop_square'))",
    "button[aria-haspopup='menu']:has(i.google-symbols:text('crop_portrait'))",
    "button[aria-haspopup='menu']:has(i.google-symbols:text('crop_landscape'))",
    "button[aria-haspopup='menu']:has(i.google-symbols:text('crop_original'))",
)

VIDEO_TAB_SELECTORS = (
    "[role='tab'][id$='-trigger-VIDEO']",
    "[role='tab'][aria-controls$='-content-VIDEO']",
    "[role='menu'] [role='tab']:has(i:text('play_circle'))",
    "[role='tab']:has-text('Video')",
    "[role='tab']:has-text('Vídeo')",
)

VIDEO_SUBMODE_SELECTORS = {
    "frames": (
        "[role='tab'][id$='-trigger-VIDEO_FRAMES']",
        "[role='menu'] [role='tab']:has(i.google-symbols:text('crop_free'))",
        "[role='tab']:has-text('Frames')",
    ),
    "references": (
        "[role='tab'][id$='-trigger-VIDEO_REFERENCES']",
        "[role='menu'] [role='tab']:has(i.google-symbols:text('chrome_extension'))",
        "[role='tab']:has-text('Ingredients')",
        "[role='tab']:has-text('References')",
        "[role='tab']:has-text('Elementos')",
    ),
}

ASPECT_SELECTORS = {
    "portrait": (
        "[role='tab'][id$='-trigger-PORTRAIT']",
        "[role='menu'] [role='tab']:has(i.google-symbols:text-is('crop_9_16'))",
        "[role='tab']:has-text('9:16')",
        "[role='tab']:has-text('Portrait')",
    ),
    "landscape": (
        "[role='tab'][id$='-trigger-LANDSCAPE']",
        "[role='menu'] [role='tab']:has(i.google-symbols:text-is('crop_16_9'))",
        "[role='tab']:has-text('16:9')",
        "[role='tab']:has-text('Landscape')",
    ),
}

FRAME_SLOTS_STRUCT = "div[type='button'][aria-haspopup='dialog']"
ADD_MEDIA_BUTTON = "button[aria-haspopup='dialog']:has(i.google-symbols:text-is('add_2'))"
UPLOAD_MEDIA_BUTTON = "[role='dialog'][data-state='open'] button:has(i.google-symbols:text-is('upload'))"
UPLOAD_MEDIA_BUTTON_TEXT = "[role='dialog'][data-state='open'] button:has-text('Upload media')"
ADD_TO_PROMPT_DIALOG = "[role='dialog'][data-state='open']"


class LocalProxyBridge:
    """
    Local Preemptive Proxy-Auth Bridge (127.0.0.1:<random_port> -> upstream HTTP proxy).
    Injects 'Proxy-Authorization: Basic ...' into the initial CONNECT / HTTP request
    so Chrome never stalls on 407 responses lacking Content-Length from upstream proxies.
    """

    def __init__(self, proxy_cfg: Optional[dict[str, str]]):
        self.proxy_cfg = proxy_cfg
        self.server: Any = None
        self.local_port: int = 0

    async def start(self) -> Optional[dict[str, str]]:
        if not self.proxy_cfg:
            return None
        srv_url = self.proxy_cfg.get("server", "")
        user = self.proxy_cfg.get("username", "")
        pw = self.proxy_cfg.get("password", "")
        if not user or not srv_url.startswith("http://"):
            return self.proxy_cfg

        parsed = urlparse(srv_url)
        up_host = parsed.hostname or ""
        up_port = int(parsed.port or 80)
        auth_b64 = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")

        async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        async def _handle_client(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
            try:
                header_bytes = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), timeout=15.0)
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(up_host, up_port), timeout=12.0)
                if b"Proxy-Authorization:" not in header_bytes:
                    injected = (
                        header_bytes[:-2]
                        + f"Proxy-Authorization: Basic {auth_b64}\r\n\r\n".encode("ascii")
                    )
                else:
                    injected = header_bytes
                up_w.write(injected)
                await up_w.drain()
                await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
            except Exception:
                try:
                    client_w.close()
                except Exception:
                    pass

        self.server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
        self.local_port = self.server.sockets[0].getsockname()[1]
        return {"server": f"http://127.0.0.1:{self.local_port}"}

    async def stop(self) -> None:
        if self.server:
            try:
                self.server.close()
                await self.server.wait_closed()
            except Exception:
                pass


class FlowAccountRunner:
    """Runs Google Flow AI video generation on an isolated Browser Profile + Dedicated Proxy IP."""

    def __init__(self, account_row: dict[str, Any]):
        self.acc = dict(account_row)
        self.acc_id = self.acc["id"]
        self.acc_name = self.acc["name"]
        self.proxy_str = self.acc.get("proxy_url", "")
        self.profile_dir = self.acc["profile_dir"]
        raw_url = (self.acc.get("flow_project_url") or "").strip()
        if not raw_url or raw_url == "https://labs.google/fx/tools/flow":
            self.flow_url = "https://flow.google.com/"
        else:
            self.flow_url = raw_url
        os.makedirs(self.profile_dir, exist_ok=True)

    def _clean_profile_locks(self) -> None:
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lp = os.path.join(self.profile_dir, lock_name)
            if os.path.exists(lp):
                try:
                    os.remove(lp)
                except Exception:
                    pass

    async def _launch_clean_chrome(
        self, p: Any, proxy_cfg: Optional[dict[str, str]], headless: bool = False
    ) -> Any:
        """
        Launch genuine Google Chrome (fallback to Edge) with:
          - Headed mode (headless=False) when logging in so user sees the browser window clearly
          - Native Chrome Headless (--headless=new) when generating scenes in background after login
          - ignore_default_args=['--enable-automation'] (hides 'controlled by automated test software' banner)
          - anti-detection webdriver patch so Google Sign-In works smoothly
        """
        self._clean_profile_locks()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1280,860",
            "--window-position=80,40",
        ]
        if headless:
            args.append("--headless=new")

        launch_kwargs: dict[str, Any] = {
            "user_data_dir": self.profile_dir,
            "headless": False,  # We use --headless=new in args when headless=True for full WebGL/reCAPTCHA compatibility
            "no_viewport": not headless,
            "ignore_default_args": ["--enable-automation"],
            "args": args,
        }
        if headless:
            launch_kwargs["viewport"] = {"width": 1440, "height": 900}
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg

        try:
            ctx = await p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        except Exception:
            try:
                ctx = await p.chromium.launch_persistent_context(channel="msedge", **launch_kwargs)
            except Exception:
                ctx = await p.chromium.launch_persistent_context(**launch_kwargs)

        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return ctx

    async def open_for_manual_login(self) -> None:
        """Open VISIBLE Google Chrome window (NEVER headless) onto https://flow.google.com/ so user can log in."""
        from playwright.async_api import async_playwright

        raw_proxy_cfg = parse_proxy(self.proxy_str)
        add_log(f"🔐 Đang mở CỬA SỐ TRÌNH DUYỆT (Hiện trực tiếp) cho Acc [{self.acc_name}] | Proxy: {self.proxy_str or 'Direct IP'}")

        # Verify IP in background via Python requests
        ip_res = check_proxy_ip(self.proxy_str, timeout=8)
        if ip_res.get("ok") and ip_res.get("ip"):
            ip_val = ip_res["ip"]
            add_log(f"🌐 Acc [{self.acc_name}] đang chạy trên IP thực tế: {ip_val}")
            with get_conn() as conn:
                conn.execute("UPDATE accounts SET last_ip = ? WHERE id = ?", (ip_val, self.acc_id))
                conn.commit()

        bridge = LocalProxyBridge(raw_proxy_cfg)
        effective_proxy = await bridge.start()

        try:
            async with async_playwright() as p:
                # ALWAYS headless=False for manual login so the window pops up on user's screen!
                ctx = await self._launch_clean_chrome(p, effective_proxy, headless=False)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                try:
                    await page.bring_to_front()
                except Exception:
                    pass

                try:
                    await page.goto(self.flow_url, wait_until="domcontentloaded", timeout=35000)
                except Exception as e:
                    add_log(f"⚠️ Đang tải trang {self.flow_url}: {e}")

                add_log(
                    f"✅ Đã hiển thị trình duyệt Chrome ({self.flow_url}) cho Acc [{self.acc_name}]! "
                    f"Anh hãy đăng nhập tài khoản Google và mở vào Project. Link Project (/project/...) ở BẤT KỲ TAB NÀO cũng sẽ được tự động bắt và lưu ngay lập tức!"
                )

                # Wait until user closes the browser window, scanning ALL open tabs continuously
                last_saved_url = self.flow_url
                try:
                    while len(ctx.pages) > 0:
                        for pg in list(ctx.pages):
                            try:
                                u = pg.url or ""
                            except Exception:
                                continue
                            # Priority 1: Direct Project URL (e.g. https://flow.google.com/project/09495e9a-...)
                            if "/project/" in u and ("flow.google" in u or "labs.google" in u):
                                clean_u = u.split("?")[0]
                                self.flow_url = clean_u
                                if clean_u != last_saved_url:
                                    last_saved_url = clean_u
                                    with get_conn() as conn:
                                        conn.execute(
                                            "UPDATE accounts SET flow_project_url = ?, status = 'logged_in' WHERE id = ?",
                                            (self.flow_url, self.acc_id),
                                        )
                                        conn.commit()
                                    add_log(f"🎯 Đã bắt tự động Link Project từ Tab trình duyệt: {self.flow_url}")
                                # Also check credit banner passively on this project tab
                                try:
                                    await self._inspect_credits_on_page(pg, raise_on_exhausted=False)
                                except Exception:
                                    pass
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

                with get_conn() as conn:
                    conn.execute(
                        "UPDATE accounts SET flow_project_url = ?, status = CASE WHEN status = 'out_of_credits' THEN 'out_of_credits' ELSE 'logged_in' END WHERE id = ?",
                        (self.flow_url, self.acc_id),
                    )
                    conn.commit()
                add_log(f"💾 Đã lưu Cookie & Project URL cho Acc [{self.acc_name}] (Sẵn sàng chạy ngầm): {self.flow_url}")
        finally:
            await bridge.stop()

    async def _inspect_credits_on_page(self, page: Any, raise_on_exhausted: bool = True) -> str:
        """
        Inspect the Google Flow page for credit warnings or credit exhaustion.
        Updates `accounts.credits_info` and `accounts.status` in SQLite.
        Raises CreditExhaustedError if hard exhaustion is detected and raise_on_exhausted=True.
        """
        try:
            body_txt = await page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return ""

        low_txt = (body_txt or "").lower()

        # Hard exhaustion phrases (Vietnamese & English)
        exhausted_phrases = (
            "bạn đã dùng hết tín dụng",
            "đã hết tín dụng google flow",
            "không đủ tín dụng",
            "không còn tín dụng",
            "you've run out of google flow credits",
            "you have run out of credits",
            "out of google flow credits",
            "not enough credits",
            "insufficient credits",
            "resource_exhausted",
        )
        for ph in exhausted_phrases:
            if ph in low_txt:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE accounts SET status = 'out_of_credits', credits_info = '🚫 Hết tín dụng (0 credit)' WHERE id = ?",
                        (self.acc_id,),
                    )
                    conn.commit()
                if raise_on_exhausted:
                    raise CreditExhaustedError(
                        f"Tài khoản [{self.acc_name}] đã hết tín dụng Google Flow ({ph})!"
                    )
                return "exhausted"

        # Low-credit warning banner (e.g. "Bạn sắp dùng hết tín dụng Google Flow...")
        low_credit_phrases = (
            "bạn sắp dùng hết tín dụng",
            "sắp hết tín dụng",
            "running low on google flow credits",
            "running low on credits",
        )
        for ph in low_credit_phrases:
            if ph in low_txt:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE accounts SET credits_info = '⚠️ Sắp hết tín dụng' WHERE id = ?",
                        (self.acc_id,),
                    )
                    conn.commit()
                return "low"

        return "ok"

    async def _configure_agent_settings_panel(
        self,
        page: Any,
        aspect: str = "portrait",
        model_name: str = "Omni 1.1 Flash",
        video_count: str = "x1",
        confirm_mode: str = "never",
    ) -> bool:
        """
        Automates the exact 'Cài đặt tác nhân' (Agent Settings) sidebar on https://flow.google.com/project/...
        as shown in the user's screenshot:
          1. Opens 'Cài đặt tác nhân' via top-right gear icon if not already open.
          2. Sets 'Xác nhận trước khi tạo' -> 'Không bao giờ' (or 'Luôn luôn').
          3. Sets 'Chế độ mặc định của tính năng tạo video' -> Aspect ('9:16' or '16:9'), Count ('x1'..'x4'), Model ('Omni 1.1 Flash', etc.).
          4. Clicks 'Lưu' (Save).
        """
        target_ratio_str = "9:16" if aspect == "portrait" else "16:9"
        target_count_str = video_count if video_count in ("x1", "x2", "x3", "x4") else "x1"

        # Step 1: Check if 'Cài đặt tác nhân' / 'Agent settings' panel is already open;
        # if not, click the Sliders ('tune') icon button inside the bottom prompt bar (right next to the '→' send button, as circled in red by the user!)
        panel_open = await page.evaluate(
            """() => {
                const txt = document.body ? document.body.innerText : '';
                return txt.includes('Cài đặt tác nhân') || txt.includes('Agent settings') || txt.includes('Chế độ mặc định của tính năng tạo video');
            }"""
        )
        if not panel_open:
            tune_clicked = await page.evaluate(
                """() => {
                    // 1) Look for Material icon 'tune' / 'instant_mix' / 'sliders' in the bottom prompt bar
                    const iconNames = ['tune', 'instant_mix', 'sliders', 'page_info', 'display_settings', 'equalizer', 'settings_input_component'];
                    const icons = [...document.querySelectorAll('mat-icon, i, span.google-symbols, span.material-icons, span.material-symbols-outlined')];
                    const bottomTuneIcon = icons.find(el => {
                        const r = el.getBoundingClientRect();
                        const t = (el.textContent || '').trim().toLowerCase();
                        return r.width > 0 && r.top > window.innerHeight * 0.55 && iconNames.includes(t);
                    });
                    if (bottomTuneIcon) {
                        const btn = bottomTuneIcon.closest('button, [role="button"]') || bottomTuneIcon;
                        btn.click();
                        return true;
                    }

                    // 2) Check aria-label / tooltip on buttons in the bottom prompt bar
                    const bottomBtns = [...document.querySelectorAll('button, [role="button"]')].filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && r.top > window.innerHeight * 0.65 && r.left > window.innerWidth * 0.25 && r.right < window.innerWidth * 0.78;
                    });
                    const tooltipBtn = bottomBtns.find(b => {
                        const lbl = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('mattooltip') || '') + ' ' + (b.title || '')).toLowerCase();
                        return lbl.includes('tác nhân') || lbl.includes('cài đặt') || lbl.includes('agent') || lbl.includes('setting') || lbl.includes('tune') || lbl.includes('tuỳ chỉnh');
                    });
                    if (tooltipBtn && !(tooltipBtn.innerText || '').trim().includes('Tác nhân')) {
                        tooltipBtn.click();
                        return true;
                    }

                    // 3) Positional match in the bottom prompt bar: the button immediately to the left of the '→' (send/arrow_forward) button!
                    if (bottomBtns.length >= 2) {
                        // Sort buttons in the bottom row of the prompt box from left to right
                        const sorted = bottomBtns
                            .filter(b => b.getBoundingClientRect().width < 70) // exclude the wide 'Tác nhân' pill
                            .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                        if (sorted.length >= 2) {
                            const slidersBtn = sorted[sorted.length - 2]; // second from right (right before '→')
                            slidersBtn.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if tune_clicked:
                await asyncio.sleep(1.2)
                panel_open = await page.evaluate(
                    """() => {
                        const txt = document.body ? document.body.innerText : '';
                        return txt.includes('Cài đặt tác nhân') || txt.includes('Agent settings') || txt.includes('Chế độ mặc định của tính năng tạo video');
                    }"""
                )

        if not panel_open:
            return False

        add_log(
            f"⚙️ Đang đồng bộ 'Cài đặt tác nhân' từ Dashboard: Xác nhận=[{confirm_mode.upper()}] | Khung Video=[{target_ratio_str}] | Số lượng=[{target_count_str}] | Model=[{model_name}]"
        )

        # Step 2: Select 'Không bao giờ' (Never confirm) or 'Luôn luôn' (Always confirm)
        want_never = confirm_mode != "always"
        await page.evaluate(
            """(wantNever) => {
                const targets = wantNever ? ['Không bao giờ', 'Never', 'Tác nhân sẽ tự động tạo'] : ['Luôn luôn', 'Always'];
                const allEls = [...document.querySelectorAll('mat-radio-button, [role="radio"], label, div, span')];
                for (const t of targets) {
                    const match = allEls.find(el => (el.textContent || '').trim().startsWith(t));
                    if (match) {
                        const clickable = match.closest('mat-radio-button, [role="radio"], label') || match;
                        clickable.click();
                        const input = clickable.querySelector('input[type="radio"]');
                        if (input) input.click();
                        break;
                    }
                }
            }""",
            want_never,
        )
        await asyncio.sleep(0.4)

        # Step 3: Click Aspect Ratio ('9:16' or '16:9') and Count ('x1'..'x4') inside the panel
        # Note: Both Image section and Video section have 16:9 / 9:16 and x1..x4 buttons.
        # Clicking all visible matching buttons inside the settings panel sets BOTH Image & Video to the user's desired ratio and count!
        await page.evaluate(
            """({ratioStr, countStr}) => {
                const clickMatchingButtons = (exactText) => {
                    const candidates = [...document.querySelectorAll('button, [role="button"], [role="tab"], mat-button-toggle, div[tabindex]')];
                    for (const el of candidates) {
                        const rect = el.getBoundingClientRect();
                        // Must be on the right-side panel (x > window.innerWidth * 0.55)
                        if (rect.width > 0 && rect.height > 0 && rect.left > window.innerWidth * 0.5) {
                            const clean = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (clean === exactText || clean.endsWith(' ' + exactText)) {
                                el.click();
                            }
                        }
                    }
                };
                clickMatchingButtons(ratioStr);
                clickMatchingButtons(countStr);
            }""",
            {"ratioStr": target_ratio_str, "countStr": target_count_str},
        )
        await asyncio.sleep(0.5)

        # Step 4: Select Video Model dropdown (e.g., 'Omni 1.1 Flash', 'Veo 3.1 - Fast', 'Veo 3.1 - Quality')
        if model_name:
            opened_dropdown = await page.evaluate(
                """() => {
                    const candidates = [...document.querySelectorAll('mat-select, [role="combobox"], button, div[aria-haspopup="listbox"]')];
                    const rightSide = candidates.filter(el => {
                        const r = el.getBoundingClientRect();
                        const txt = (el.innerText || '').trim();
                        return r.width > 0 && r.left > window.innerWidth * 0.5 &&
                               (txt.includes('Omni') || txt.includes('Veo') || txt.includes('Flash') || txt.includes('Quality'));
                    });
                    if (rightSide.length > 0) {
                        rightSide[rightSide.length - 1].click();
                        return true;
                    }
                    return false;
                }"""
            )
            if opened_dropdown:
                await asyncio.sleep(0.6)
                await page.evaluate(
                    """(targetModel) => {
                        const opts = [...document.querySelectorAll('mat-option, [role="option"], [role="menuitem"], li, button')];
                        const m = opts.find(o => (o.innerText || '').toLowerCase().includes(targetModel.toLowerCase()));
                        if (m) m.click();
                        else {
                            // Close listbox if not found
                            document.body.click();
                        }
                    }""",
                    model_name.split("-")[0].strip(),
                )
                await asyncio.sleep(0.5)

        # Step 5: Click 'Lưu' (Save) button at the bottom of 'Cài đặt tác nhân'
        saved = await page.evaluate(
            """() => {
                const btns = [...document.querySelectorAll('button, [role="button"]')];
                const saveBtn = btns.find(b => {
                    const txt = (b.innerText || b.textContent || '').trim();
                    const r = b.getBoundingClientRect();
                    return r.width > 0 && r.left > window.innerWidth * 0.5 && (txt === 'Lưu' || txt === 'Save');
                });
                if (saveBtn) {
                    saveBtn.click();
                    return true;
                }
                return false;
            }"""
        )
        if saved:
            add_log(f"✅ Đã bấm [Lưu] trong bảng 'Cài đặt tác nhân' ({target_ratio_str} | {target_count_str} | {model_name})!")
            await asyncio.sleep(1.0)
        return True

    async def generate_scene_8s(
        self, scene: dict[str, Any], project: dict[str, Any], headless_mode: bool = True
    ) -> str:
        """
        Generate a single 8-second scene on Google Flow AI using this account's isolated IP & saved cookies.
        Runs in Headless background mode by default once cookies are saved.
        """
        from playwright.async_api import async_playwright

        raw_proxy_cfg = parse_proxy(self.proxy_str)
        scene_id = scene["id"]
        scene_order = scene["scene_order"]
        aspect = project.get("aspect_ratio", "portrait")
        model_name = project.get("model_name", "Veo 3.1 - Fast")
        gen_mode = project.get("gen_mode", "references")

        ref_media = (scene.get("ref_media_path") or "").strip() or (project.get("global_ref_media") or "").strip()
        ref_desc = (scene.get("ref_description") or "").strip() or (project.get("global_ref_desc") or "").strip()
        final_prompt = compose_scene_prompt(
            scene_prompt=scene["scene_prompt"],
            ref_description=ref_desc,
            global_ref_desc=project.get("global_ref_desc", ""),
            duration_sec=8,
        )

        ref_files: list[str] = []
        if ref_media and os.path.exists(ref_media):
            ref_files = extract_reference_frames_from_video(ref_media, max_frames=2)

        mode_label = "Chạy Ngầm (Headless)" if headless_mode else "Hiện Trình Duyệt"
        add_log(
            f"🎬 [Cảnh #{scene_order} - 8s] Bắt đầu trên Acc [{self.acc_name}] ({mode_label}) | "
            f"Chế độ: {gen_mode.upper()} | File tham chiếu: {len(ref_files)}"
        )

        current_ip = "Direct"
        ip_res = check_proxy_ip(self.proxy_str, timeout=10)
        if ip_res.get("ok") and ip_res.get("ip"):
            current_ip = ip_res["ip"]
            with get_conn() as conn:
                conn.execute("UPDATE accounts SET last_ip = ? WHERE id = ?", (current_ip, self.acc_id))
                conn.execute("UPDATE scenes SET account_ip_used = ? WHERE id = ?", (current_ip, scene_id))
                conn.commit()
            add_log(f"🌐 Acc [{self.acc_name}] xác nhận IP độc lập: {current_ip}")

        bridge = LocalProxyBridge(raw_proxy_cfg)
        effective_proxy = await bridge.start()

        try:
            async with async_playwright() as p:
                ctx = await self._launch_clean_chrome(p, effective_proxy, headless=headless_mode)

                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                    # 2. Navigate directly to Google Flow (https://flow.google.com/project/...)
                    target_url = self.flow_url or "https://flow.google.com/"
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(4.0)

                    # If on landing page instead of inside a project, click "New project" / "Dự án mới" / "+"
                    if "/project/" not in page.url:
                        for btn_txt in ["New project", "Dự án mới", "Create with Flow", "Bắt đầu", "Start", "add"]:
                            btn = page.locator("button, a").filter(has_text=btn_txt).first
                            if await btn.count() > 0:
                                try:
                                    await btn.click(timeout=5000)
                                    await asyncio.sleep(3.5)
                                    if "/project/" in page.url:
                                        self.flow_url = page.url.split("?")[0]
                                        with get_conn() as conn:
                                            conn.execute(
                                                "UPDATE accounts SET flow_project_url = ? WHERE id = ?",
                                                (self.flow_url, self.acc_id),
                                            )
                                            conn.commit()
                                        add_log(f"🎯 Đã tự động vào và lưu Link Project mới: {self.flow_url}")
                                        break
                                except Exception:
                                    pass

                    # 2b. Check if this account has run out of credits before starting generation
                    await self._inspect_credits_on_page(page, raise_on_exhausted=True)

                    # Count existing videos before generation
                    before_vids = await self._get_all_video_urls(page)
                    add_log(f"📊 Số video hiện có trong Project: {len(before_vids)}")

                    # 3. Configure 'Cài đặt tác nhân' (Agent Settings sidebar from screenshot) + Classic Flow settings
                    agent_cfg_ok = await self._configure_agent_settings_panel(
                        page=page,
                        aspect=aspect,
                        model_name=model_name,
                        video_count=project.get("video_count", "x1"),
                        confirm_mode=project.get("confirm_mode", "never"),
                    )
                    if not agent_cfg_ok:
                        await self._configure_flow_settings(
                            page=page,
                            gen_mode=gen_mode if ref_files else "text",
                            aspect=aspect,
                            model_name=model_name,
                            duration_sec=8,
                        )

                    # 4. Attach Reference Image(s) / Reference Video Keyframes if provided
                    if ref_files:
                        add_log(f"📎 Đang đính kèm {len(ref_files)} ảnh/frame tham chiếu vào Flow...")
                        if gen_mode == "frames":
                            await self._attach_frame_slot(page, ref_files[0], slot_index=0)
                            if len(ref_files) > 1:
                                await self._attach_frame_slot(page, ref_files[1], slot_index=1)
                        else:
                            await self._attach_reference_ingredients(page, ref_files)

                    # 5. Fill Composed Prompt (Reference Description + Scene Prompt)
                    add_log(f"✍️ Đang nhập Prompt (8s): {final_prompt[:110]}...")
                    filled = await self._fill_prompt_box(page, final_prompt)
                    if not filled:
                        raise RuntimeError("Không tìm thấy ô nhập Prompt trên giao diện Google Flow (hãy kiểm tra đăng nhập)!")

                    # 6. Attach network listener & click real Submit button (mints valid reCAPTCHA Enterprise token)
                    gen_triggered = {"ok": False, "err": "", "credit_exhausted": False}

                    def on_resp(resp: Any) -> None:
                        if any(
                            k in resp.url
                            for k in (
                                "batchAsyncGenerateVideo",
                                "batchexecute",
                                "aisandbox",
                            )
                        ):
                            if resp.status in (200, 201):
                                gen_triggered["ok"] = True
                            elif resp.status in (402, 429):
                                gen_triggered["credit_exhausted"] = True
                                gen_triggered["err"] = f"Hết tín dụng / Quota (HTTP {resp.status})"
                            elif resp.status in (401, 403):
                                gen_triggered["err"] = f"HTTP {resp.status} từ Flow API"

                    page.on("response", on_resp)
                    clicked = await self._click_submit_button(page)
                    if not clicked:
                        raise RuntimeError("Không bấm được nút Create / Gửi trên Google Flow!")

                    add_log(f"🚀 Đã bấm Create cho Cảnh #{scene_order} (8s)! Đang đợi Google Flow render video...")

                    # 7. Poll for newly generated video URL (timeout up to 300s)
                    new_vid_url = await self._wait_for_new_video_url(page, set(before_vids), timeout_s=300, gen_state=gen_triggered)
                    if not new_vid_url:
                        if gen_triggered.get("credit_exhausted"):
                            with get_conn() as conn:
                                conn.execute(
                                    "UPDATE accounts SET status = 'out_of_credits', credits_info = '🚫 Hết tín dụng' WHERE id = ?",
                                    (self.acc_id,),
                                )
                                conn.commit()
                            raise CreditExhaustedError(f"Tài khoản [{self.acc_name}] đã hết tín dụng ({gen_triggered['err']})!")
                        if gen_triggered["err"]:
                            raise RuntimeError(f"Google Flow từ chối yêu cầu: {gen_triggered['err']}")
                        raise RuntimeError("Hết thời gian chờ (300s) nhưng chưa nhận được video mới từ Google Flow.")

                    # 8. Download the generated 8s MP4 video
                    proj_slug = f"project_{project['id']}"
                    proj_out_dir = os.path.join(OUTPUTS_DIR, proj_slug)
                    os.makedirs(proj_out_dir, exist_ok=True)
                    out_mp4 = os.path.join(proj_out_dir, f"scene_{scene_order:03d}_8s.mp4")

                    add_log(f"⬇️ Đang tải video Cảnh #{scene_order} (8s) về máy...")
                    await self._download_video_via_context(ctx, new_vid_url, out_mp4)
                    add_log(f"✅ Hoàn tất Cảnh #{scene_order} (8s) -> {out_mp4}")
                    return out_mp4

                finally:
                    await ctx.close()
        finally:
            await bridge.stop()

    async def _configure_flow_settings(
        self, page: Any, gen_mode: str, aspect: str, model_name: str, duration_sec: int = 8
    ) -> None:
        """Open Flow's settings menu and select Video mode, sub-mode, aspect ratio, 8s duration, x1 count."""
        # Open settings menu via gflow-cli crop_* trigger or flow-py pill text fallback
        opened = False
        for sel in MODE_SWITCH_TRIGGER_SELECTORS:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                try:
                    await loc.click(timeout=4000)
                    await asyncio.sleep(0.8)
                    opened = True
                    break
                except Exception:
                    pass

        if not opened:
            # Fallback from flow-py: click button containing x1/x2 or Video/Image/Veo
            await page.evaluate(
                """() => {
                    const btns = [...document.querySelectorAll('button')];
                    const pill = btns.find(b => /x[1-4]/.test(b.textContent) || b.textContent.includes('Veo') || b.textContent.includes('Video'));
                    if (pill) pill.click();
                }"""
            )
            await asyncio.sleep(0.8)

        # 1) Select VIDEO tab
        for sel in VIDEO_TAB_SELECTORS:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                try:
                    await loc.click(timeout=3000)
                    await asyncio.sleep(0.5)
                    break
                except Exception:
                    pass

        # 2) Select Video Sub-Mode ('references' for R2V Ingredients or 'frames' for I2V Start/End Frame)
        if gen_mode in VIDEO_SUBMODE_SELECTORS:
            for sel in VIDEO_SUBMODE_SELECTORS[gen_mode]:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    try:
                        await loc.click(timeout=3000)
                        await asyncio.sleep(0.6)
                        break
                    except Exception:
                        pass

        # 3) Select Aspect Ratio (portrait 9:16 or landscape 16:9)
        for sel in ASPECT_SELECTORS.get(aspect, ASPECT_SELECTORS["portrait"]):
            loc = page.locator(sel).first
            if await loc.count() > 0:
                try:
                    await loc.click(timeout=3000)
                    await asyncio.sleep(0.4)
                    break
                except Exception:
                    pass

        # 4) Select 8s Duration tab ('8s')
        for sel in (
            f"[role='tab']:text-is('{duration_sec}s')",
            f"[role='tab']:has-text('{duration_sec}s')",
            f"button:text-is('{duration_sec}s')",
            f"button:has-text('{duration_sec}s')",
        ):
            loc = page.locator(sel).first
            if await loc.count() > 0:
                try:
                    await loc.click(timeout=2500)
                    await asyncio.sleep(0.4)
                    break
                except Exception:
                    pass

        # 5) Select Count x1 so we don't burn extra credits per scene
        for sel in ("[role='tab']:text-is('x1')", "[role='tab']:text-is('1x')", "button:text-is('x1')"):
            loc = page.locator(sel).first
            if await loc.count() > 0:
                try:
                    await loc.click(timeout=2500)
                    await asyncio.sleep(0.3)
                    break
                except Exception:
                    pass

        # Close menu by pressing Escape
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            pass

    async def _attach_frame_slot(self, page: Any, image_path: str, slot_index: int = 0) -> None:
        """Attach Start Frame (slot 0) or End Frame (slot 1) in I2V Frames mode."""
        slots = page.locator(FRAME_SLOTS_STRUCT)
        if await slots.count() > slot_index:
            await slots.nth(slot_index).click(timeout=5000)
            await asyncio.sleep(1.0)
            await self._upload_file_in_open_dialog(page, image_path)
        else:
            await self._attach_reference_ingredients(page, [image_path])

    async def _attach_reference_ingredients(self, page: Any, image_paths: list[str]) -> None:
        """Attach reference images in R2V Ingredients / References mode or new Flow Agent '+' prompt button."""
        for img_p in image_paths[:3]:
            # Priority 1: Direct input[type='file'] if present in DOM
            direct_input = page.locator("input[type='file']").first
            if await direct_input.count() > 0:
                try:
                    await direct_input.set_input_files(img_p)
                    await asyncio.sleep(3.0)
                    continue
                except Exception:
                    pass

            clicked_add = False
            for sel in (
                ADD_MEDIA_BUTTON,
                "button:has(i:text-is('add'))",
                "button:has(mat-icon:text-is('add'))",
                "button:has-text('Add Media')",
                "button:has-text('Thêm phương tiện')",
                "button[aria-haspopup='dialog']",
            ):
                loc = page.locator(sel).last
                if await loc.count() > 0:
                    try:
                        await loc.click(timeout=4000)
                        await asyncio.sleep(1.0)
                        clicked_add = True
                        break
                    except Exception:
                        pass
            if clicked_add:
                await self._upload_file_in_open_dialog(page, img_p)

    async def _upload_file_in_open_dialog(self, page: Any, file_path: str) -> None:
        """Upload file via Flow's open media dialog and click 'Add to Prompt'."""
        # Try direct file chooser on Upload Media button (from gflow-cli)
        uploaded = False
        for sel in (UPLOAD_MEDIA_BUTTON, UPLOAD_MEDIA_BUTTON_TEXT, "button:has-text('Upload')", "button:has-text('Tải lên')"):
            loc = page.locator(sel).first
            if await loc.count() > 0:
                try:
                    async with page.expect_file_chooser(timeout=6000) as fc_info:
                        await loc.click(timeout=4000)
                    fc = await fc_info.value
                    await fc.set_files(file_path)
                    uploaded = True
                    await asyncio.sleep(3.5)
                    break
                except Exception:
                    pass

        if not uploaded:
            file_input = page.locator("input[type='file']").first
            if await file_input.count() > 0:
                await file_input.set_input_files(file_path)
                await asyncio.sleep(3.5)

        # Click 'Add to Prompt' (the iconless button inside the open dialog, per gflow-cli)
        add_btn = (
            page.locator(ADD_TO_PROMPT_DIALOG)
            .last.locator("button")
            .filter(has_not=page.locator("i.google-symbols"))
            .last
        )
        if await add_btn.count() > 0:
            try:
                await add_btn.click(timeout=4000)
                await asyncio.sleep(1.5)
            except Exception:
                pass

    async def _fill_prompt_box(self, page: Any, prompt: str) -> bool:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass

        async def _try_fill() -> bool:
            els = page.locator("div[role='textbox'][data-slate-editor='true'], div[contenteditable='true'], textarea")
            count = await els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    vis = await el.evaluate("e => e.offsetWidth > 0 && e.offsetHeight > 0 && e.getBoundingClientRect().y > 80")
                    if not vis:
                        continue
                    await el.scroll_into_view_if_needed()
                    await el.click(timeout=3000)
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(0.1)
                    await page.keyboard.insert_text(prompt)
                    await asyncio.sleep(0.2)
                    await page.keyboard.type(" ", delay=20)
                    return True
                except Exception:
                    continue
            return False

        if await _try_fill():
            return True

        # If on the new Flow Agent canvas ("Bắt đầu tạo hoặc thả nội dung nghe nhìn"), click the center prompt trigger
        await page.evaluate(
            """() => {
                const triggers = ['Bắt đầu tạo', 'Start creating', 'thả nội dung nghe nhìn', 'Type a prompt', 'Nhập câu lệnh'];
                const all = [...document.querySelectorAll('div, p, span, button')];
                for (const t of triggers) {
                    const el = all.find(e => (e.innerText || '').trim().includes(t) && e.getBoundingClientRect().y > 120);
                    if (el) { el.click(); break; }
                }
            }"""
        )
        await asyncio.sleep(0.8)
        return await _try_fill()

    async def _click_submit_button(self, page: Any) -> bool:
        clicked = await page.evaluate(
            """() => {
                const btns = [...document.querySelectorAll('button')];
                const creates = btns.filter(b =>
                    !b.disabled && (
                        b.textContent.trim().includes('Create') ||
                        b.textContent.trim().includes('Tạo') ||
                        b.textContent.trim().includes('Gửi') ||
                        b.textContent.trim().includes('Send') ||
                        b.textContent.trim().includes('arrow_forward') ||
                        b.textContent.trim().includes('arrow_upward') ||
                        b.textContent.trim().includes('send')
                    )
                );
                if (creates.length > 0) {
                    creates[creates.length - 1].click();
                    return true;
                }
                return false;
            }"""
        )
        if clicked:
            return True
        btn = page.locator("button:has(i:text('arrow_forward')), button:has(i:text('arrow_upward')), button:has(i:text('send'))").last
        if await btn.count() > 0:
            await btn.click()
            return True
        # Fallback: press Enter in the focused prompt textbox
        try:
            await page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    async def _get_all_video_urls(self, page: Any) -> list[str]:
        urls = await page.evaluate(
            """() => {
                const vids = [...document.querySelectorAll('video')];
                return vids.map(v => v.src || v.currentSrc || '').filter(u => u && (u.startsWith('http') || u.startsWith('blob:')));
            }"""
        )
        return urls or []

    async def _wait_for_new_video_url(
        self, page: Any, existing_urls: set[str], timeout_s: int = 300, gen_state: Optional[dict[str, Any]] = None
    ) -> Optional[str]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if gen_state and gen_state.get("credit_exhausted"):
                return None

            # Check for credit exhaustion on DOM during generation
            await self._inspect_credits_on_page(page, raise_on_exhausted=True)

            # Check for policy error
            body_txt = (await page.evaluate("() => document.body.innerText || ''")).lower()
            if any(err in body_txt for err in ("violates our policy", "unable to generate", "couldn't generate", "vi phạm chính sách")):
                raise RuntimeError("Prompt hoặc ảnh tham chiếu bị bộ lọc chính sách của Google Flow từ chối!")

            curr_urls = await self._get_all_video_urls(page)
            for u in reversed(curr_urls):
                if u not in existing_urls and not u.startswith("blob:"):
                    return u
            await asyncio.sleep(3.0)
        return None

    async def _download_video_via_context(self, ctx: Any, video_url: str, out_path: str) -> None:
        resp = await ctx.request.get(video_url, timeout=60000)
        if resp.status != 200:
            raise RuntimeError(f"Tải video thất bại (HTTP {resp.status})")
        data = await resp.body()
        with open(out_path, "wb") as f:
            f.write(data)


def merge_project_scenes_ffmpeg(project_id: int) -> str:
    """Merge all completed 8s scene MP4s of a project into a single master MP4 video."""
    with get_conn() as conn:
        scenes = conn.execute(
            "SELECT * FROM scenes WHERE project_id = ? AND status = 'completed' ORDER BY scene_order ASC",
            (project_id,),
        ).fetchall()

    valid_files = [s["video_path"] for s in scenes if s["video_path"] and os.path.exists(s["video_path"])]
    if not valid_files:
        raise RuntimeError("Chưa có cảnh 8s nào hoàn thành để ghép video!")

    proj_dir = os.path.join(OUTPUTS_DIR, f"project_{project_id}")
    os.makedirs(proj_dir, exist_ok=True)
    concat_txt = os.path.join(proj_dir, "concat_list.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for vp in valid_files:
            safe_p = vp.replace("\\", "/")
            f.write(f"file '{safe_p}'\n")

    merged_out = os.path.join(proj_dir, f"FULL_VIDEO_PROJECT_{project_id}_{int(time.time())}.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_txt,
        "-c",
        "copy",
        merged_out,
    ]
    subprocess.run(cmd, check=True, timeout=120)

    with get_conn() as conn:
        conn.execute("UPDATE projects SET merged_video_path = ? WHERE id = ?", (merged_out, project_id))
        conn.commit()

    add_log(f"🎉 Đã ghép {len(valid_files)} cảnh (mỗi cảnh 8s) thành video hoàn chỉnh: {merged_out}")
    return merged_out
