#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
XServer VPS 自动续期脚本（2025 Turnstile 特化版）
- 针对 Cloudflare Turnstile 2025 版做专门优化
- 重点：
  * 利用 Playwright frame 遍历，直接在 Turnstile iframe 内点击复选框/按钮
  * 多轮人类鼠标轨迹 + 停顿 + 点击
  * WebGL / navigator / permissions 等反检测补丁
  * 验证码 OCR + 自动刷新重试
  * JST 到期日判断、Telegram 通知、README 状态输出
"""

import asyncio
import re
import datetime
from datetime import timezone, timedelta
import os
import json
import logging
from typing import Optional, Dict
import random

from playwright.async_api import async_playwright

# 尝试兼容两种 playwright-stealth 版本（可选）
try:
    from playwright_stealth import stealth_async
    STEALTH_VERSION = 'old'
except ImportError:
    STEALTH_VERSION = 'new'
    stealth_async = None


# ======================== 配置 ==========================

class Config:
    LOGIN_EMAIL = os.getenv("XSERVER_EMAIL")
    LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD")
    VPS_ID = os.getenv("XSERVER_VPS_ID", "40124478")

    # 为了 Turnstile，这里即使配置了 true 也会强制用非无头
    USE_HEADLESS = os.getenv("USE_HEADLESS", "true").lower() == "true"
    WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "30000"))

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # 仅支持带 schema 的简单代理，如 socks5://ip:port 或 http://ip:port
    PROXY_SERVER = os.getenv("PROXY_SERVER")

    CAPTCHA_API_URL = os.getenv(
        "CAPTCHA_API_URL",
        "https://captcha-120546510085.asia-northeast1.run.app"
    )

    DETAIL_URL = f"https://secure.xserver.ne.jp/xapanel/xvps/server/detail?id={VPS_ID}"
    EXTEND_URL = f"https://secure.xserver.ne.jp/xapanel/xvps/server/freevps/extend/index?id_vps={VPS_ID}"


# ======================== 日志 ==========================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('renewal.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ======================== 通用工具 ==========================

async def human_move(page, start=(100, 100), end=(300, 300), steps: int = 20):
    """伪随机人类鼠标轨迹"""
    x1, y1 = start
    x2, y2 = end
    for i in range(steps):
        t = (i + 1) / steps
        nx = x1 + (x2 - x1) * t + random.uniform(-2, 2)
        ny = y1 + (y2 - y1) * t + random.uniform(-2, 2)
        await page.mouse.move(nx, ny)
        await asyncio.sleep(random.uniform(0.01, 0.08))


# ======================== 通知器 ==========================

class Notifier:
    @staticmethod
    async def send_telegram(message: str):
        if not all([Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID]):
            return
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                "chat_id": Config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as resp:
                    if resp.status == 200:
                        logger.info("✅ Telegram 通知发送成功")
                    else:
                        logger.error(f"❌ Telegram 返回非 200 状态码: {resp.status}")
        except Exception as e:
            logger.error(f"❌ Telegram 发送失败: {e}")

    @staticmethod
    async def notify(subject: str, message: str):
        # 目前只使用 Telegram（subject 预留）
        await Notifier.send_telegram(message)


# ======================== 验证码识别 ==========================

class CaptchaSolver:
    """外部 API OCR 验证码识别器"""

    def __init__(self):
        self.api_url = Config.CAPTCHA_API_URL

    def _validate_code(self, code: str) -> bool:
        """验证识别出的验证码是否合理"""
        if not code:
            return False

        if len(code) < 4 or len(code) > 6:
            logger.warning(f"⚠️ 验证码长度异常: {len(code)} 位")
            return False

        if len(set(code)) == 1:
            logger.warning(f"⚠️ 验证码可疑(所有数字相同): {code}")
            return False

        if not code.isdigit():
            logger.warning(f"⚠️ 验证码包含非数字字符: {code}")
            return False

        return True

    async def solve(self, img_data_url: str) -> Optional[str]:
        """使用外部 API 识别验证码"""
        try:
            import aiohttp

            logger.info(f"📤 发送验证码到 API: {self.api_url}")

            max_retries = 3
            retry_count = 0

            while retry_count < max_retries:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            self.api_url,
                            data=img_data_url,
                            headers={'Content-Type': 'text/plain'},
                            timeout=aiohttp.ClientTimeout(total=20)
                        ) as resp:
                            if not resp.ok:
                                raise Exception(f"API 请求失败: {resp.status}")

                            code_response = await resp.text()
                            code = code_response.strip()

                            logger.info(f"📥 API 返回验证码: {code}")

                            if code and len(code) >= 4:
                                numbers = re.findall(r'\d+', code)
                                if numbers:
                                    code = numbers[0][:6]

                                    if self._validate_code(code):
                                        logger.info(f"🎯 API 识别成功: {code}")
                                        return code

                            raise Exception('API 返回无效验证码')

                except Exception as err:
                    retry_count += 1
                    if retry_count >= max_retries:
                        logger.error(f"❌ API 识别失败(已重试 {max_retries} 次): {err}")
                        return None
                    logger.info(f"🔄 验证码识别失败,正在进行第 {retry_count} 次重试...")
                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"❌ API 识别错误: {e}")

        return None


# ======================== 核心类 ==========================

class XServerVPSRenewal:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None  # 保存 playwright 实例，方便关闭

        self.renewal_status: str = "Unknown"
        self.old_expiry_time: Optional[str] = None
        self.new_expiry_time: Optional[str] = None
        self.error_message: Optional[str] = None

        self.captcha_solver = CaptchaSolver()

    # ---------- 缓存 ----------
    def load_cache(self) -> Optional[Dict]:
        if os.path.exists("cache.json"):
            try:
                with open("cache.json", "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载缓存失败: {e}")
        return None

    def save_cache(self):
        cache = {
            "last_expiry": self.old_expiry_time,
            "status": self.renewal_status,
            "last_check": datetime.datetime.now(timezone.utc).isoformat(),
            "vps_id": Config.VPS_ID
        }
        try:
            with open("cache.json", "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")

    # ---------- 截图 ----------
    async def shot(self, name: str):
        """安全截图, 不影响主流程"""
        if not self.page:
            return
        try:
            await self.page.screenshot(path=f"{name}.png", full_page=True)
        except Exception:
            pass

    # ---------- 访问限制检测 ----------
    async def check_access_restriction(self):
        """检测是否被 XServer / CF 做访问限制"""
        try:
            html = await self.page.content()
            block_keywords = [
                "アクセス制限",
                "アクセスが制限されています",
                "403 Forbidden",
                "セキュリティ上の理由によりアクセスできません",
                "your access has been blocked",
            ]
            if any(k in html for k in block_keywords):
                raise Exception("访问被限制（可能是 IP/代理/行为异常导致）")
        except Exception as e:
            raise e

    # ---------- 浏览器 ----------
    async def setup_browser(self) -> bool:
        try:
            self._pw = await async_playwright().start()
            launch_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-infobars",
                "--start-maximized",
                "--disable-gpu",
                "--use-gl=swiftshader",
                "--use-angle=swiftshader",
            ]

            proxy_url = None
            if Config.PROXY_SERVER:
                proxy_url = Config.PROXY_SERVER
                logger.info(f"🌐 使用代理: {Config.PROXY_SERVER}")

            if Config.USE_HEADLESS:
                logger.info("⚠️ 为了通过 Turnstile，强制使用非无头模式(headless=False)")
            else:
                logger.info("ℹ️ 已配置非无头模式(headless=False)")

            if proxy_url:
                launch_args.append(f"--proxy-server={proxy_url}")

            launch_kwargs = {
                "headless": False,
                "args": launch_args
            }

            self.browser = await self._pw.chromium.launch(**launch_kwargs)

            context_options = {
                "viewport": {"width": 1920, "height": 1080},
                "locale": "ja-JP",
                "timezone_id": "Asia/Tokyo",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }

            self.context = await self.browser.new_context(**context_options)

            # Anti-bot 注入：去掉 webdriver、补全 plugins / languages / permissions、修补 WebGL
            await self.context.add_init_script("""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP','zh-CN','en-US']});
Object.defineProperty(navigator, 'permissions', {
    get: () => ({
        query: ({name}) => Promise.resolve({state: 'granted'})
    })
});
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) { return "Intel Inc."; }              // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) { return "Intel Iris OpenGL Engine"; } // UNMASKED_RENDERER_WEBGL
    return _getParameter.call(this, parameter);
};
""")

            self.page = await self.context.new_page()
            self.page.set_default_timeout(Config.WAIT_TIMEOUT)

            if STEALTH_VERSION == 'old' and stealth_async is not None:
                await stealth_async(self.page)
            else:
                logger.info("ℹ️ 未安装 playwright_stealth 或使用新版，跳过 stealth 处理")

            logger.info("✅ 浏览器初始化成功")
            return True
        except Exception as e:
            logger.error(f"❌ 浏览器初始化失败: {e}")
            self.error_message = str(e)
            return False

    # ---------- 登录 ----------
    async def login(self) -> bool:
        try:
            logger.info("🌐 开始登录")
            await self.page.goto(
                "https://secure.xserver.ne.jp/xapanel/login/xvps/",
                timeout=30000
            )
            await self.check_access_restriction()
            await asyncio.sleep(2)
            await self.shot("01_login")

            await self.page.fill("input[name='memberid']", Config.LOGIN_EMAIL)
            await self.page.fill("input[name='user_password']", Config.LOGIN_PASSWORD)
            await self.shot("02_before_submit")

            logger.info("📤 提交登录表单...")
            await self.page.click("input[type='submit']")
            await asyncio.sleep(5)
            await self.shot("03_after_submit")

            await self.check_access_restriction()

            if "xvps/index" in self.page.url or "login" not in self.page.url.lower():
                logger.info("🎉 登录成功")
                return True

            logger.error("❌ 登录失败")
            self.error_message = "登录失败"
            return False
        except Exception as e:
            logger.error(f"❌ 登录错误: {e}")
            self.error_message = f"登录错误: {e}"
            return False

    # ---------- 获取到期时间 ----------
    async def get_expiry(self) -> bool:
        try:
            await self.page.goto(Config.DETAIL_URL, timeout=30000)
            await self.check_access_restriction()
            await asyncio.sleep(3)
            await self.shot("04_detail")

            expiry_date = await self.page.evaluate("""
                () => {
                    const rows = document.querySelectorAll('tr');
                    for (const row of rows) {
                        const text = row.innerText || row.textContent;
                        if (text.includes('利用期限') && !text.includes('利用開始')) {
                            const match = text.match(/(\\d{4})年(\\d{1,2})月(\\d{1,2})日/);
                            if (match) return {year: match[1], month: match[2], day: match[3]};
                        }
                    }
                    return null;
                }
            """)

            if expiry_date:
                self.old_expiry_time = (
                    f"{expiry_date['year']}-"
                    f"{expiry_date['month'].zfill(2)}-"
                    f"{expiry_date['day'].zfill(2)}"
                )
                logger.info(f"📅 利用期限: {self.old_expiry_time}")
                return True

            logger.warning("⚠️ 未能解析利用期限")
            return False
        except Exception as e:
            logger.error(f"❌ 获取到期时间失败: {e}")
            return False

    # ---------- 点击"更新する" ----------
    async def click_update(self) -> bool:
        try:
            try:
                await self.page.click("a:has-text('更新する')", timeout=3000)
                await asyncio.sleep(2)
                logger.info("✅ 点击更新按钮(链接)")
                return True
            except Exception:
                pass

            try:
                await self.page.click("button:has-text('更新する')", timeout=3000)
                await asyncio.sleep(2)
                logger.info("✅ 点击更新按钮(按钮)")
                return True
            except Exception:
                pass

            selectors = [
                "button:has-text('更新')",
                "a:has-text('更新')",
                "text=更新する",
                "button:has-text('継続')",
                "a:has-text('継続')",
            ]
            for sel in selectors:
                try:
                    await self.page.click(sel, timeout=2000)
                    await asyncio.sleep(2)
                    logger.info(f"✅ 点击更新按钮(兼容选择器: {sel})")
                    return True
                except Exception:
                    continue

            logger.info("ℹ️ 未找到更新按钮")
            return False
        except Exception as e:
            logger.info(f"ℹ️ 点击更新按钮失败: {e}")
            return False

    # ---------- 打开续期页面 ----------
    async def open_extend(self) -> bool:
        try:
            await asyncio.sleep(2)
            await self.shot("05_before_extend")

            try:
                logger.info("🔍 方法1: 查找续期按钮(按钮)...")
                await self.page.click(
                    "button:has-text('引き続き無料VPSの利用を継続する')",
                    timeout=3000
                )
                await asyncio.sleep(5)
                await self.check_access_restriction()
                await self.shot("06_extend_page")
                logger.info("✅ 打开续期页面(按钮点击成功)")
                return True
            except Exception as e1:
                logger.info(f"ℹ️ 方法1失败(按钮): {e1}")

            try:
                logger.info("🔍 方法1b: 尝试链接形式...")
                await self.page.click(
                    "a:has-text('引き続き無料VPSの利用を継続する')",
                    timeout=3000
                )
                await asyncio.sleep(5)
                await self.check_access_restriction()
                await self.shot("06_extend_page")
                logger.info("✅ 打开续期页面(链接点击成功)")
                return True
            except Exception as e1b:
                logger.info(f"ℹ️ 方法1b失败(链接): {e1b}")

            try:
                logger.info("🔍 方法2: 直接访问续期URL...")
                await self.page.goto(Config.EXTEND_URL, timeout=Config.WAIT_TIMEOUT)
                await self.check_access_restriction()
                await asyncio.sleep(3)
                await self.shot("05_extend_url")

                content = await self.page.content()

                if "引き続き無料VPSの利用を継続する" in content:
                    try:
                        await self.page.click(
                            "button:has-text('引き続き無料VPSの利用を継続する')",
                            timeout=5000
                        )
                        await asyncio.sleep(5)
                        await self.shot("06_extend_page")
                        logger.info("✅ 打开续期页面(方法2-按钮)")
                        return True
                    except Exception:
                        await self.page.click(
                            "a:has-text('引き続き無料VPSの利用を継続する')",
                            timeout=5000
                        )
                        await asyncio.sleep(5)
                        await self.shot("06_extend_page")
                        logger.info("✅ 打开续期页面(方法2-链接)")
                        return True

                if "延長期限" in content or "期限まで" in content:
                    logger.info("ℹ️ 未到续期时间窗口")
                    self.renewal_status = "Unexpired"
                    return False

            except Exception as e2:
                logger.info(f"ℹ️ 方法2失败: {e2}")

            logger.warning("⚠️ 所有打开续期页面的方法都失败")
            return False

        except Exception as e:
            logger.warning(f"⚠️ 打开续期页面异常: {e}")
            return False

    # ---------- Turnstile 高级处理（2025 特化版） ----------
    async def _click_turnstile_in_frames(self) -> bool:
        """
        在所有 frame 中寻找 Turnstile 相关的 checkbox / 按钮并点击。
        直接使用 frame.click，而不是鼠标全局坐标。
        """
        selectors = [
            "input[type='checkbox']",
            "[role='checkbox']",
            "label[for]",
            "div[role='button']",
            "button",
        ]

        target_frames = []
        for frame in self.page.frames:
            url = frame.url or ""
            if "turnstile" in url or "challenges" in url or "cloudflare" in url:
                target_frames.append(frame)

        # 如果没找到明显的 Turnstile frame，就退一步在所有 frame 中找
        frames_to_scan = target_frames or self.page.frames
        logger.info(f"📋 Turnstile frame 数量: {len(frames_to_scan)} (标记: {len(target_frames)})")

        for frame in frames_to_scan:
            for sel in selectors:
                try:
                    locator = frame.locator(sel)
                    if await locator.count() == 0:
                        continue

                    handle = locator.first
                    try:
                        await handle.scroll_into_view_if_needed()
                    except Exception:
                        pass

                    logger.info(f"🖱️ 在 frame({frame.url}) 中点击元素: {sel}")
                    # 使用 delay 模拟人类点击
                    await handle.click(delay=random.randint(80, 200))
                    await asyncio.sleep(2)
                    return True
                except Exception:
                    continue

        return False

    async def complete_turnstile_verification(self, max_wait: int = 120) -> bool:
        """多策略完成 Cloudflare Turnstile 验证（2025 特化版）"""
        try:
            logger.info("🔐 开始 Cloudflare Turnstile 验证流程...")

            has_turnstile = await self.page.evaluate("""
                () => {
                    const sel = [
                        '.cf-turnstile',
                        '#turnstile-container',
                        '[data-sitekey]',
                        'iframe[src*="turnstile"]'
                    ];
                    return sel.some(s => document.querySelector(s));
                }
            """)

            if not has_turnstile and not any(
                "turnstile" in (f.url or "") for f in self.page.frames
            ):
                logger.info("ℹ️ 未检测到 Turnstile, 跳过验证")
                return True

            logger.info("🔍 检测到 Turnstile, 尝试多种方法触发验证...")

            # 初始鼠标行为
            try:
                await human_move(self.page, (50, 50), (200, 200), steps=20)
                await asyncio.sleep(0.5)
            except Exception:
                pass

            # 一开始先尝试 frame 内点击
            await self._click_turnstile_in_frames()
            await self.shot("07_turnstile_clicked_once")

            # 滚动页面
            try:
                await self.page.evaluate("window.scrollBy(0, 250)")
                await asyncio.sleep(0.5)
                await self.page.evaluate("window.scrollBy(0, -150)")
            except Exception:
                pass

            logger.info("⏳ 等待 Turnstile 验证完成...")

            for i in range(max_wait):
                await asyncio.sleep(1)

                verification_status = await self.page.evaluate("""
                    () => {
                        const tokenField = document.querySelector('[name="cf-turnstile-response"]');
                        const hasToken = tokenField && tokenField.value && tokenField.value.length > 0;

                        const pageText = document.body.innerText || document.body.textContent || '';
                        const hasSuccessText = pageText.includes('成功しました') || pageText.includes('成功');

                        const container = document.querySelector('.cf-turnstile');
                        let hasCheckmark = false;
                        if (container) {
                            hasCheckmark = container.classList.contains('success') ||
                                           container.classList.contains('verified') ||
                                           container.querySelector('[aria-checked="true"]') !== null;
                        }

                        return {
                            hasToken: hasToken,
                            hasSuccessText: hasSuccessText,
                            hasCheckmark: hasCheckmark,
                            tokenLength: hasToken ? tokenField.value.length : 0,
                            verified: hasToken || hasSuccessText || hasCheckmark
                        };
                    }
                """)

                if verification_status["verified"]:
                    logger.info(
                        "✅ Turnstile 验证成功! "
                        f"(令牌:{verification_status['hasToken']}, "
                        f"文本:{verification_status['hasSuccessText']}, "
                        f"对勾:{verification_status['hasCheckmark']})"
                    )
                    await self.shot("07_turnstile_success")
                    return True

                # 每 15 秒再尝试点击一次 frame
                if i in (5, 15, 30, 45, 60, 75):
                    logger.info(f"🔄 第 {i} 秒，重新尝试在 frame 中点击 Turnstile...")
                    await self._click_turnstile_in_frames()

                if i % 10 == 0 and i > 0:
                    status_parts = []
                    if not verification_status['hasToken']:
                        status_parts.append("等待令牌")
                    if not verification_status['hasSuccessText']:
                        status_parts.append("等待成功标志")
                    if not verification_status['hasCheckmark']:
                        status_parts.append("等待对勾")
                    logger.info(
                        f"⏳ Turnstile 验证中... ({i}/{max_wait}秒) "
                        f"[{', '.join(status_parts) if status_parts else '检查中'}]"
                    )

            logger.warning(f"⚠️ Turnstile 验证超时({max_wait}秒)")
            await self.shot("07_turnstile_timeout")

            final_status = await self.page.evaluate("""
                () => {
                    const tokenField = document.querySelector('[name="cf-turnstile-response"]');
                    return {
                        hasToken: tokenField && tokenField.value && tokenField.value.length > 0,
                        tokenValue: tokenField && tokenField.value
                            ? tokenField.value.substring(0, 30) + '...'
                            : 'empty'
                    };
                }
            """)

            if final_status['hasToken']:
                logger.info(f"⚠️ 超时但检测到令牌({final_status['tokenValue']}),尝试继续")
                return True

            return False

        except Exception as e:
            logger.error(f"❌ Turnstile 验证失败: {e}")
            return False

    # ---------- 提交续期表单 ----------
    async def submit_extend(self) -> bool:
        """提交续期表单 - 先完成 Turnstile, 再处理验证码并提交"""
        try:
            logger.info("📄 开始提交续期表单")
            await asyncio.sleep(3)

            logger.info("👤 在续期页面模拟用户行为以辅助 Turnstile 通过...")
            try:
                await human_move(self.page, (50, 50), (200, 160), steps=20)
                await asyncio.sleep(0.6)
                await self.page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.8)
                await self.page.evaluate("window.scrollBy(0, -200)")
                await asyncio.sleep(0.6)
            except Exception:
                pass

            logger.info("🔐 步骤1: 完成 Turnstile 验证...")
            turnstile_success = await self.complete_turnstile_verification(max_wait=90)

            if not turnstile_success:
                logger.warning("⚠️ Turnstile 验证未完全确认,但继续尝试提交...")

            await asyncio.sleep(2)

            # 步骤 2: 验证码图片
            logger.info("🔍 步骤2: 查找验证码图片...")
            img_data_url = await self.page.evaluate("""
                () => {
                    const candidates = [
                        'img[src^="data:image"]',
                        'img[src^="data:"]',
                        'img[alt*="画像認証"]',
                        'img[alt*="認証"]',
                        'img'
                    ];
                    for (const sel of candidates) {
                        const img = document.querySelector(sel);
                        if (img && img.src) return img.src;
                    }
                    throw new Error('未找到验证码图片');
                }
            """)

            if not img_data_url:
                logger.info("ℹ️ 无验证码,可能未到续期时间")
                self.renewal_status = "Unexpired"
                return False

            logger.info("📸 已找到验证码图片,正在发送到 API 进行识别...")
            await self.shot("08_captcha_found")

            code = await self.captcha_solver.solve(img_data_url)

            if not code:
                logger.warning("⚠️ 验证码识别失败，尝试刷新验证码...")
                try:
                    await self.page.click("img", timeout=2000)
                    await asyncio.sleep(2)
                except Exception:
                    pass

                new_img_data = await self.page.evaluate("""
                    () => {
                        const img = document.querySelector('img');
                        return img && img.src ? img.src : null;
                    }
                """)
                if new_img_data:
                    logger.info("🔁 已刷新验证码，再次识别...")
                    await self.shot("08_captcha_refreshed")
                    code = await self.captcha_solver.solve(new_img_data)

            if not code:
                logger.error("❌ 验证码识别失败（重试后仍失败）")
                self.renewal_status = "Failed"
                self.error_message = "验证码识别失败"
                return False

            logger.info(f"⌨️ 步骤3: 填写验证码: {code}")
            input_filled = await self.page.evaluate("""
                (code) => {
                    const candidates = [
                      '[placeholder*="上の画像"]',
                      'input[name*="captcha"]',
                      'input[type="text"]',
                      'input'
                    ];
                    let input = null;
                    for (const sel of candidates) {
                        const el = document.querySelector(sel);
                        if (el) { input = el; break; }
                    }
                    if (!input) {
                        throw new Error('未找到验证码输入框');
                    }
                    input.value = code;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            """, code)

            if not input_filled:
                raise Exception("未找到验证码输入框")

            await asyncio.sleep(2)
            await self.shot("09_captcha_filled")

            try:
                await human_move(self.page, (270, 300), (420, 260), steps=20)
                await asyncio.sleep(0.7)
            except Exception:
                pass

            logger.info("🔍 步骤4: 最终确认 Turnstile 令牌...")
            final_check = await self.page.evaluate("""
                () => {
                    const tokenField = document.querySelector('[name="cf-turnstile-response"]');
                    const successText = (document.body.innerText || document.body.textContent || '');
                    return {
                        hasToken: tokenField && tokenField.value && tokenField.value.length > 0,
                        tokenLength: tokenField && tokenField.value ? tokenField.value.length : 0,
                        hasSuccessText: successText.includes('成功')
                    };
                }
            """)

            if final_check['hasToken']:
                logger.info(
                    f"✅ Turnstile 令牌确认 (长度: {final_check['tokenLength']}, "
                    f"成功标志: {final_check['hasSuccessText']})"
                )
            else:
                logger.warning("⚠️ Turnstile 令牌缺失,提交可能失败")

            await asyncio.sleep(1)

            logger.info("🖱️ 步骤5: 提交表单...")
            await self.shot("10_before_submit")

            submitted = await self.page.evaluate("""
                () => {
                    if (typeof window.submit_button !== 'undefined' &&
                        window.submit_button &&
                        typeof window.submit_button.click === 'function') {
                        window.submit_button.click();
                        return true;
                    }
                    const selectors = [
                        'input[type="submit"]',
                        'button[type="submit"]',
                        'button:has-text("送信")',
                        'button:has-text("確定")',
                        'button:has-text("継続")',
                        'button:has-text("更新する")'
                    ];
                    for (const sel of selectors) {
                        const btn = document.querySelector(sel);
                        if (btn) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)

            if not submitted:
                logger.error("❌ 无法提交表单")
                raise Exception("无法提交表单")

            logger.info("✅ 表单已提交")
            await asyncio.sleep(5)
            await self.shot("11_after_submit")

            html = await self.page.content()

            if any(err in html for err in [
                "入力された認証コードが正しくありません",
                "認証コードが正しくありません",
                "エラー",
                "間違",
                "失敗しました"
            ]):
                logger.error("❌ 验证码错误或 Turnstile 验证失败")
                await self.shot("11_error")
                self.renewal_status = "Failed"
                self.error_message = "验证码错误或 Turnstile 验证失败"
                return False

            if any(success in html for success in [
                "完了",
                "継続",
                "完成",
                "更新しました",
                "更新が完了しました"
            ]):
                logger.info("🎉 续期成功")
                self.renewal_status = "Success"
                await self.get_expiry()
                self.new_expiry_time = self.old_expiry_time
                return True

            logger.warning("⚠️ 续期提交结果未知")
            self.renewal_status = "Unknown"
            return False

        except Exception as e:
            logger.error(f"❌ 续期错误: {e}")
            self.renewal_status = "Failed"
            self.error_message = str(e)
            return False

    # ---------- README 生成 ----------
    def generate_readme(self):
        now = datetime.datetime.now(timezone(timedelta(hours=8)))  # 显示为 UTC+8
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        out = "# XServer VPS 自动续期状态\n\n"
        out += f"**运行时间**: `{ts} (UTC+8)`<br>\n"
        out += f"**VPS ID**: `{Config.VPS_ID}`<br>\n\n---\n\n"

        if self.renewal_status == "Success":
            out += (
                "## ✅ 续期成功\n\n"
                f"- 🕛 **旧到期**: `{self.old_expiry_time}`\n"
                f"- 🕡 **新到期**: `{self.new_expiry_time}`\n"
            )
        elif self.renewal_status == "Unexpired":
            out += (
                "## ℹ️ 尚未到期\n\n"
                f"- 🕛 **到期时间**: `{self.old_expiry_time}`\n"
            )
        else:
            out += (
                "## ❌ 续期失败\n\n"
                f"- 🕛 **到期**: `{self.old_expiry_time or '未知'}`\n"
                f"- ⚠️ **错误**: {self.error_message or '未知'}\n"
            )

        out += f"\n---\n\n*最后更新: {ts}*\n"

        with open("README.md", "w", encoding="utf-8") as f:
            f.write(out)

        logger.info("📄 README.md 已更新")

    # ---------- 主流程 ----------
    async def run(self):
        try:
            logger.info("=" * 60)
            logger.info("🚀 XServer VPS 自动续期开始")
            logger.info("=" * 60)

            if not await self.setup_browser():
                self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", f"浏览器初始化失败: {self.error_message}")
                return

            if not await self.login():
                self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", f"登录失败: {self.error_message}")
                return

            await self.get_expiry()

            try:
                if self.old_expiry_time:
                    today_jst = datetime.datetime.now(timezone(timedelta(hours=9))).date()
                    expiry_date = datetime.datetime.strptime(
                        self.old_expiry_time, "%Y-%m-%d"
                    ).date()
                    can_extend_date = expiry_date - datetime.timedelta(days=1)

                    logger.info(f"📅 今日日期(JST): {today_jst}")
                    logger.info(f"📅 到期日期: {expiry_date}")
                    logger.info(f"📅 可续期开始日: {can_extend_date}")

                    if today_jst < can_extend_date:
                        logger.info("ℹ️ 当前 VPS 尚未到可续期时间，无需续期。")
                        self.renewal_status = "Unexpired"
                        self.error_message = None

                        self.save_cache()
                        self.generate_readme()

                        await Notifier.notify(
                            "ℹ️ 尚未到续期日",
                            f"当前利用期限: {self.old_expiry_time}\n"
                            f"可续期开始日: {can_extend_date}"
                        )
                        return
                    else:
                        logger.info("✅ 已达到可续期日期，继续执行续期流程...")
                else:
                    logger.warning("⚠️ 未获取到 old_expiry_time，跳过自动判断逻辑")
            except Exception as e:
                logger.error(f"❌ 自动判断是否需要续期失败: {e}")

            await self.page.goto(Config.DETAIL_URL, timeout=Config.WAIT_TIMEOUT)
            await asyncio.sleep(2)
            await self.check_access_restriction()
            await self.click_update()
            await asyncio.sleep(3)

            opened = await self.open_extend()
            if not opened and self.renewal_status == "Unexpired":
                self.generate_readme()
                await Notifier.notify("ℹ️ 尚未到期", f"当前到期时间: {self.old_expiry_time}")
                return
            elif not opened:
                self.renewal_status = "Failed"
                self.error_message = "无法打开续期页面"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", "无法打开续期页面")
                return

            await self.submit_extend()

            self.save_cache()
            self.generate_readme()

            if self.renewal_status == "Success":
                await Notifier.notify("✅ 续期成功", f"续期成功，新到期时间: {self.new_expiry_time}")
            elif self.renewal_status == "Unexpired":
                await Notifier.notify("ℹ️ 尚未到期", f"当前到期时间: {self.old_expiry_time}")
            else:
                await Notifier.notify("❌ 续期失败", f"错误信息: {self.error_message or '未知错误'}")

        finally:
            logger.info("=" * 60)
            logger.info(f"✅ 流程完成 - 状态: {self.renewal_status}")
            logger.info("=" * 60)
            try:
                if self.page:
                    await self.page.close()
                if self.context:
                    await self.context.close()
                if self.browser:
                    await self.browser.close()
                if self._pw:
                    await self._pw.stop()
                logger.info("🧹 浏览器已关闭")
            except Exception as e:
                logger.warning(f"关闭浏览器时出错: {e}")


async def main():
    runner = XServerVPSRenewal()
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
