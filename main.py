#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import platform
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ================== 配置 ==================
BASE_URL = "https://panel.freemcserver.net"
LOGIN_URL = f"{BASE_URL}/user/login"
SERVER_INDEX_URL = f"{BASE_URL}/server/index"

OUTPUT_DIR = Path("output/screenshots")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("freemcserver-keepalive")

# ================== 辅助函数 ==================
def is_linux() -> bool:
    return platform.system().lower() == "linux"

def mask_email(email: str) -> str:
    if '@' not in email:
        return email[:1] + "***"
    local, domain = email.split('@', 1)
    masked_local = local[:1] + "***" if local else "***"
    if '.' in domain:
        parts = domain.split('.')
        tld = parts[-1]
        first_char = domain[0]
        masked_domain = f"{first_char}***.{tld}" if len(parts) > 1 else f"{first_char}***"
    else:
        masked_domain = domain[:1] + "***"
    return f"{masked_local}@{masked_domain}"

def mask_server_id(server_id: str) -> str:
    if len(server_id) <= 4:
        return server_id
    return server_id[:2] + "***" + server_id[-1]

def mask_server_name(server_name: str, server_id: str) -> str:
    if server_id in server_name:
        return server_name.replace(server_id, mask_server_id(server_id))
    return server_name

def mask_url(url: str) -> str:
    return re.sub(r'/server/\d+', '/server/***', url)

def setup_display():
    if is_linux() and not os.environ.get("DISPLAY"):
        try:
            from pyvirtualdisplay import Display
            display = Display(visible=False, size=(1920, 1080))
            display.start()
            os.environ["DISPLAY"] = display.new_display_var
            logger.info("虚拟显示已启动")
            return display
        except Exception as e:
            logger.error(f"虚拟显示启动失败: {e}")
            sys.exit(1)
    return None

def screenshot_path(account_index: int, name: str) -> str:
    return str(OUTPUT_DIR / f"{datetime.now().strftime('%H%M%S')}-acc{account_index}-{name}.png")

def safe_screenshot(sb, path: str, result: Optional[Dict] = None):
    try:
        sb.save_screenshot(path)
        logger.info(f"📸 截图 → {Path(path).name}")
        if result is not None:
            result.setdefault("screenshots", []).append(path)
    except Exception as e:
        logger.warning(f"截图失败: {e}")

def notify_telegram(account_index: int, email: str, server_results: List[Dict], overall_success: bool, overall_message: str = "", screenshot_file: str = None):
    try:
        token = os.environ.get("TG_BOT_TOKEN")
        chat_id = os.environ.get("TG_CHAT_ID")
        if not token or not chat_id:
            return

        status = "✅ 续订成功" if overall_success else "❌ 续订失败"
        text = f"{status}\n\n账号：{email}\n"
        
        if server_results:
            for sr in server_results:
                server_id = sr.get("id", "未知")
                before = sr.get("before", "")
                after = sr.get("after", "")
                started = sr.get("started", False)
                
                text += f"服务器：{server_id}\n"
                if started:
                    text += "启动成功\n"
                if before and after:
                    text += f"到期: {before} -> {after}\n"
                elif after:
                    text += f"到期: {after}\n"
                elif before:
                    text += f"到期: {before}\n"
        
        text += f"\nFreeMcServer Auto Renew"

        if screenshot_file and Path(screenshot_file).exists():
            with open(screenshot_file, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": text},
                    files={"photo": f},
                    timeout=60
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=30
            )
    except Exception as e:
        logger.warning(f"Telegram 通知失败: {e}")

def parse_accounts() -> List[Tuple[str, str]]:
    raw = os.environ.get("FREEMCSERVER", "")
    accounts = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "-----" in line:
            parts = line.split("-----", 1)
            email = parts[0].strip()
            password = parts[1].strip()
            if email and password:
                accounts.append((email, password))
                logger.info(f"发现账号: {mask_email(email)}")
            else:
                logger.warning(f"账号格式错误（邮箱或密码为空）: {line}")
        else:
            logger.warning(f"账号格式错误（缺少 -----）: {line}")
    return accounts

# ================== CDP 拦截 AdBlocker ==================
def enable_cdp_adblock_interception(sb):
    """使用 Chrome DevTools Protocol 拦截 adblock-detected 请求"""
    try:
        sb.execute_cdp_cmd("Network.enable", {})
        sb.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": [
                "*adblock-detected*",
                "*site/adblock-detected*"
            ]
        })
        logger.info("✅ 已启用 CDP 拦截（AdBlocker）")
        return True
    except Exception as e:
        logger.warning(f"CDP 拦截失败: {e}")
        return False

# ================== 通过页面操作检查并启动服务器 ==================
def check_and_start_server(sb, server_id: str) -> bool:
    """
    访问服务器管理页面，检查服务器状态，如果关机则启动

    Returns:
        True: 成功启动了关机的服务器
        False: 未启动（服务器运行中或启动失败）
    """
    try:
        server_url = f"{BASE_URL}/server/{server_id}"
        logger.info(f"访问服务器管理页: {mask_url(server_url)}")

        sb.open(server_url)
        time.sleep(3)

        sb.execute_script("""
            (function() {
                var headers = document.querySelectorAll('.card-header .card-title');
                for (var i = 0; i < headers.length; i++) {
                    if (headers[i].innerText.includes('Server IPs')) {
                        headers[i].scrollIntoView({behavior: 'smooth', block: 'center'});
                        return;
                    }
                }
                window.scrollTo(0, 0);
            })();
        """)
        time.sleep(2)

        server_status = sb.execute_script("""
            (function() {
                var result = { status: null, status_text: '' };
                var statusBadge = document.querySelector('.server-status');
                if (statusBadge) {
                    var statusText = statusBadge.innerText || '';
                    result.status_text = statusText;
                    if (statusText.includes('Server is stopped')) {
                        result.status = 'stopped';
                    } else if (statusText.includes('Server is online')) {
                        result.status = 'online';
                    }
                }
                return result;
            })();
        """)

        status = server_status.get('status')
        status_text = server_status.get('status_text', '')

        if not status:
            logger.warning(f"无法获取服务器状态（状态文本: {status_text}），跳过启动检查")
            return False

        logger.info(f"服务器状态: {status_text}")

        if status == 'online':
            logger.info(f"✅ 服务器 {mask_server_id(server_id)} 运行中")
            return False

        logger.info(f"🔴 服务器 {mask_server_id(server_id)} 已关机，准备启动...")

        sb.execute_script("""
            (function() {
                var headers = document.querySelectorAll('.card-header h4');
                for (var i = 0; i < headers.length; i++) {
                    if (headers[i].innerText.includes('Server Control')) {
                        headers[i].scrollIntoView({behavior: 'smooth', block: 'center'});
                        return;
                    }
                }
            })();
        """)
        time.sleep(2)

        start_clicked = sb.execute_script("""
            (function() {
                var startBtn = document.querySelector('#server-control-start');
                if (startBtn) {
                    var style = window.getComputedStyle(startBtn);
                    if (style.display !== 'none' && !startBtn.disabled) {
                        startBtn.click();
                        return true;
                    }
                }
                return false;
            })();
        """)

        if not start_clicked:
            logger.warning("未找到或无法点击 Start 按钮")
            return False

        logger.info(f"🚀 已点击 Start 按钮，等待服务器启动...")
        time.sleep(3)

        popup_shown = False
        for i in range(15):
            has_success_popup = sb.execute_script("""
                (function() {
                    var popup = document.querySelector('.swal2-popup.swal2-modal.swal2-icon-success.swal2-show');
                    if (!popup) return false;
                    var title = document.querySelector('#swal2-title');
                    if (title && title.innerText.includes('Server started')) return true;
                    return false;
                })();
            """)

            if has_success_popup:
                popup_shown = True
                logger.info(f"✅ 服务器启动成功弹窗已显示")
                try:
                    sb.execute_script("""
                        (function() {
                            var confirmBtn = document.querySelector('.swal2-confirm');
                            if (confirmBtn) confirmBtn.click();
                        })();
                    """)
                    time.sleep(1)
                except:
                    pass
                break

            time.sleep(1)

        if popup_shown:
            logger.info(f"✅ 服务器 {mask_server_id(server_id)} 启动成功")
            return True
        else:
            time.sleep(3)
            sb.open(server_url)
            time.sleep(2)

            final_status = sb.execute_script("""
                (function() {
                    var statusBadge = document.querySelector('.server-status');
                    if (statusBadge) {
                        var text = statusBadge.innerText || '';
                        if (text.includes('Server is online')) return 'online';
                        if (text.includes('Server is stopped')) return 'stopped';
                    }
                    return null;
                })();
            """)

            if final_status == 'online':
                logger.info(f"✅ 服务器 {mask_server_id(server_id)} 启动成功（状态已变更）")
                return True
            else:
                logger.warning(f"⚠️ 服务器 {mask_server_id(server_id)} 启动状态未确认")
                return True

    except Exception as e:
        logger.warning(f"检查/启动服务器异常: {e}")
        return False

# ================== AdBlocker 统一处理 ==================
def handle_adblocker(sb, account_index: int, result: Dict, context: str = "") -> bool:
    try:
        is_adblock_page = sb.execute_script('''
            (function() {
                var title = document.title || '';
                var bodyText = document.body ? document.body.innerText : '';
                if (title.includes('Turn off your adblocker') || title.includes('adblocker')) return true;
                if (bodyText.includes('Please turn off your adblocker') ||
                    bodyText.includes('I have disabled my AdBlocker')) return true;
                if (document.querySelector('.site-adblock')) return true;
                return false;
            })();
        ''')

        if is_adblock_page:
            logger.warning(f"🚨 检测到 AdBlocker 整页警告 {context}")
            safe_screenshot(sb, screenshot_path(account_index, f"adblocker-page-{context}"), result)

            sb.execute_script('''
                (function() {
                    var adblockDiv = document.querySelector('.site-adblock');
                    if (adblockDiv) adblockDiv.remove();
                    var wrapper = document.querySelector('section#wrapper');
                    if (wrapper) wrapper.style.display = 'none';
                    var particlesJs = document.querySelector('#particles-js');
                    if (particlesJs) particlesJs.style.display = 'none';
                    document.body.style.background = 'none';
                    document.body.innerHTML = '<div style="display:none">Bypassed</div>';
                })();
            ''')

            logger.info("已移除 AdBlocker 页面，执行重定向...")
            sb.open(SERVER_INDEX_URL)
            time.sleep(3)
            return True

        has_modal = sb.execute_script('''
            (function() {
                var modal = document.querySelector('.modal.show, .modal.fade.show');
                if (!modal) return false;
                var text = modal.innerText || '';
                return text.includes('AdBlocker') || text.includes('disable my AdBlocker');
            })();
        ''')

        if has_modal:
            logger.info(f"检测到 AdBlocker 弹窗 {context}，尝试关闭...")
            safe_screenshot(sb, screenshot_path(account_index, f"adblocker-modal-{context}"), result)

            sb.execute_script('''
                (function() {
                    var modal = document.querySelector('.modal.show, .modal.fade.show');
                    if (modal) modal.remove();
                    var backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) backdrop.remove();
                    document.body.classList.remove('modal-open');
                })();
            ''')
            time.sleep(2)

        return True

    except Exception as e:
        logger.warning(f"处理 AdBlocker 异常 {context}: {e}")
        return True

# ================== Cloudflare 整页挑战 ==================
def is_cloudflare_interstitial(sb) -> bool:
    try:
        has_login_form = sb.execute_script('''
            return !!(document.querySelector('#loginformmodel-username')
                   || document.querySelector('form[action*="/user/login"]'));
        ''')
        if has_login_form:
            return False
        has_dashboard = sb.execute_script('''
            return !!(document.querySelector('.server-card')
                   || document.querySelector('.server-renew'));
        ''')
        if has_dashboard:
            return False
        page_source = sb.get_page_source()
        title = sb.get_title().lower() if sb.get_title() else ""
        indicators = ["Just a moment", "Verify you are human", "Checking your browser", "Checking if the site connection is secure"]
        for ind in indicators:
            if ind in page_source:
                return True
        if "just a moment" in title or "attention required" in title:
            return True
        body_len = sb.execute_script('return document.body ? document.body.innerText.length : 0;')
        if body_len < 200 and "challenges.cloudflare.com" in page_source:
            return True
        return False
    except:
        return False

def bypass_cloudflare_interstitial(sb, max_attempts=3) -> bool:
    logger.info("检测到 Cloudflare 整页挑战，尝试绕过...")

    for attempt in range(max_attempts):
        logger.info(f"CF 绕过尝试 {attempt+1}/{max_attempts}")
        try:
            # 等待 Cloudflare 的验证 iframe 出现（通常 id 以 "cf-chl-widget-" 开头）
            if not sb.execute_script(
                "return !!document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]');"
            ):
                # 如果没有 iframe，可能是未被加载，使用通用点击
                sb.uc_gui_click_captcha()
            else:
                # 切换到 Cloudflare 的 iframe 并点击复选框
                sb.switch_to_frame('iframe[src*="challenges.cloudflare.com"]')
                # 等待复选框可点击
                sb.wait_for_element_visible('input[type="checkbox"]', timeout=5)
                sb.click('input[type="checkbox"]')
                # 切回主页面
                sb.switch_to_default_content()
            
            # 等待验证完成
            time.sleep(6)
            if not is_cloudflare_interstitial(sb):
                logger.info("✅ Cloudflare 挑战已通过")
                return True
        except Exception as e:
            logger.warning(f"CF 绕过失败: {e}")
        
        time.sleep(3)

    logger.info("尝试刷新页面重试...")
    for _ in range(2):   # 可以多刷新几次
        try:
            sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
            time.sleep(5)
            if not is_cloudflare_interstitial(sb):
                return True
        except:
            pass

    return False

# ================== 获取服务器到期时间 ==================
def get_server_expiry(sb, server_id: str) -> Optional[str]:
    try:
        manage_url = f"{BASE_URL}/server/{server_id}"
        sb.open(manage_url)
        time.sleep(3)

        expiry = sb.execute_script('''
            (function() {
                if (window.fmcs && window.fmcs.server_expires_at) {
                    return window.fmcs.server_expires_at;
                }
                var badges = document.querySelectorAll('.badge');
                for (var i = 0; i < badges.length; i++) {
                    var text = badges[i].innerText || '';
                    if (text.includes('Server Expires on:')) {
                        var match = text.match(/(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2})/);
                        if (match) return match[1];
                    }
                }
                return null;
            })();
        ''')

        return expiry if expiry else None
    except Exception as e:
        logger.warning(f"获取服务器 {mask_server_id(server_id)} 到期时间失败: {e}")
        return None

# ================== Turnstile 处理（带刷新重试）==================
def _wait_for_turnstile_token(sb, timeout: int = 25) -> bool:
    """
    等待 Turnstile token 填充完成。
    返回 True 表示 token 已就绪，False 表示超时。
    """
    start = time.time()
    while time.time() - start < timeout:
        token_ready = sb.execute_script('''
            (function() {
                var tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                if (tokenInput && tokenInput.value && tokenInput.value.length > 20) return true;
                var renewBtn = document.querySelector('#renew-btn');
                if (renewBtn && !renewBtn.disabled) return true;
                var successEl = document.querySelector('#success');
                if (successEl && getComputedStyle(successEl).display !== 'none') return true;
                return false;
            })();
        ''')
        if token_ready:
            return True
        time.sleep(1)
    return False


def handle_turnstile_verification(sb, account_index: int, result: Dict,
                                   server_id: str = "",
                                   page_url: str = "",
                                   max_page_retries: int = 3) -> bool:
    """
    处理 Turnstile 验证。
    在每轮尝试（3 次点击 + 30 秒被动等待）失败后，
    刷新页面重新开始，最多重试 max_page_retries 轮。
    """

    def _scroll_to_turnstile():
        sb.execute_script('''
            (function() {
                var t = document.querySelector('.cf-turnstile');
                if (t) t.scrollIntoView({behavior:'smooth', block:'center'});
                else window.scrollTo(0, document.body.scrollHeight / 2);
            })();
        ''')
        time.sleep(2)

    def _has_turnstile():
        return sb.execute_script('''
            (function() {
                return !!(document.querySelector('.cf-turnstile') ||
                          document.querySelector('[data-sitekey]') ||
                          document.querySelector('iframe[src*="challenges.cloudflare"]') ||
                          document.querySelector('iframe[src*="turnstile"]'));
            })();
        ''')

    logger.info("处理 Turnstile 验证...")

    for page_round in range(1, max_page_retries + 1):
        # ── 刷新页面（第 1 轮不刷新，直接用当前页面）──────────────────────
        if page_round > 1:
            logger.info(f"🔄 Turnstile 第 {page_round} 轮：刷新续订页面后重试…")
            try:
                if page_url:
                    sb.uc_open_with_reconnect(page_url, reconnect_time=8)
                else:
                    sb.refresh()
                time.sleep(5)

                # 刷新后重新滚动 & 等待页面就绪
                sb.execute_script('window.scrollTo(0, document.body.scrollHeight);')
                time.sleep(2)
                sb.execute_script('''
                    (function() {
                        var t = document.querySelector('.cf-turnstile');
                        var b = document.querySelector('#renew-btn');
                        if (t) t.scrollIntoView({behavior:'smooth', block:'center'});
                        else if (b) b.scrollIntoView({behavior:'smooth', block:'center'});
                        else window.scrollTo(0, document.body.scrollHeight / 2);
                    })();
                ''')
                time.sleep(3)

                safe_screenshot(
                    sb,
                    screenshot_path(account_index,
                                    f"turnstile-reload-{mask_server_id(server_id)}-r{page_round}"),
                    result
                )
            except Exception as e:
                logger.warning(f"刷新页面失败: {e}")

        # ── 检测是否存在 Turnstile ────────────────────────────────────────
        _scroll_to_turnstile()
        if not _has_turnstile():
            logger.info("未检测到 Turnstile 组件，视为通过")
            return True

        logger.info(f"发现 Turnstile 组件（第 {page_round} 轮），开始点击尝试…")

        # ── 3 次主动点击 ──────────────────────────────────────────────────
        verified = False
        for attempt in range(1, 4):
            logger.info(f"  Turnstile 点击尝试 {attempt}/3（轮 {page_round}/{max_page_retries}）")
            try:
                sb.uc_gui_click_captcha()
            except Exception as e:
                logger.warning(f"  uc_gui_click_captcha 失败: {e}")

            if _wait_for_turnstile_token(sb, timeout=25):
                logger.info(f"  ✅ Turnstile 点击成功（第 {attempt} 次）")
                verified = True
                break

            # 每次点击失败后滚回组件
            _scroll_to_turnstile()

        # ── 被动等待 30 秒 ────────────────────────────────────────────────
        if not verified:
            logger.info(f"  点击均未成功，被动等待 Turnstile 自动完成（30 秒）…")
            if _wait_for_turnstile_token(sb, timeout=30):
                logger.info("  ✅ Turnstile 自动完成")
                verified = True

        # ── 本轮验证成功 ──────────────────────────────────────────────────
        if verified:
            safe_screenshot(
                sb,
                screenshot_path(account_index,
                                 f"turnstile-success-{mask_server_id(server_id)}-r{page_round}"),
                result
            )
            return True

        # ── 本轮失败，截图记录，准备下一轮刷新 ───────────────────────────
        logger.warning(
            f"⚠️ Turnstile 第 {page_round}/{max_page_retries} 轮验证失败，"
            + ("即将刷新重试…" if page_round < max_page_retries else "已达最大重试次数。")
        )
        safe_screenshot(
            sb,
            screenshot_path(account_index,
                             f"turnstile-failed-r{page_round}-{server_id}"),
            result
        )

    # ── 所有轮次均失败 ────────────────────────────────────────────────────
    logger.error("❌ Turnstile 验证失败（已用尽所有重试轮次）")
    return False

# ================== 登录 Turnstile 处理（新增） ==================
def _wait_login_turnstile_token(sb, timeout=30) -> bool:
    """等待登录 Turnstile 生成有效 token（基于 hidden input 长度）"""
    last_len = 0
    for _ in range(timeout):
        token_len = sb.execute_script('''
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            return inp ? inp.value.length : 0;
        ''')
        if token_len > 20:
            logger.info(f"登录 Turnstile token 已生成 (长度 {token_len})")
            return True
        if token_len != last_len:
            last_len = token_len
        time.sleep(1)
    logger.warning(f"超时未得到 token，最终长度 {last_len}")
    return False

def handle_login_turnstile(sb, account_index: int, result: Dict) -> bool:
    """处理登录页面的 Turnstile 验证（点击复选框，等待 token）"""
    try:
        # 等待 Turnstile 出现
        for _ in range(15):
            if sb.execute_script("return !!document.querySelector('.cf-turnstile');"):
                break
            time.sleep(1)
        else:
            logger.info("未检测到 Turnstile，跳过")
            return True

        logger.info("登录页发现 Turnstile，寻找复选框...")
        # 尝试点击复选框
        clicked = sb.execute_script('''
            var cb = document.querySelector('.cf-turnstile input[type="checkbox"]');
            if(cb){
                cb.click();
                return true;
            }
            return false;
        ''')
        if not clicked:
            # 尝试点击 label
            sb.execute_script('''
                var label = document.querySelector('.cf-turnstile label');
                if(label) label.click();
            ''')

        # 如果仍无效，使用 SeleniumBase 的通用点击验证码方法
        if not clicked:
            try:
                sb.uc_gui_click_captcha()
            except Exception as e:
                logger.warning(f"uc_gui_click_captcha 失败: {e}")

        logger.info("等待 Turnstile 完成验证...")
        if _wait_login_turnstile_token(sb, timeout=30):
            safe_screenshot(sb, screenshot_path(account_index, "login-turnstile-success"), result)
            return True

        logger.warning("登录 Turnstile 验证超时")
        safe_screenshot(sb, screenshot_path(account_index, "login-turnstile-failed"), result)
        return False
    except Exception as e:
        logger.warning(f"处理登录 Turnstile 异常: {e}")
        return False

# ================== 登录流程 ==================
def handle_initial_page(sb, account_index: int, result: Dict) -> Optional[str]:
    logger.info("访问登录页...")

    enable_cdp_adblock_interception(sb)
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8)
    time.sleep(4)
    safe_screenshot(sb, screenshot_path(account_index, "01-initial"), result)

    current_url = sb.get_current_url()
    logger.info(f"当前URL: {mask_url(current_url)}")

    if "/server/index" in current_url:
        logger.info("✅ 已经登录")
        return "already_logged"

    if not handle_adblocker(sb, account_index, result, "initial"):
        return None

    if is_cloudflare_interstitial(sb):
        if not bypass_cloudflare_interstitial(sb):
            safe_screenshot(sb, screenshot_path(account_index, "02-cf-failed"), result)
            return None
        time.sleep(3)
        current_url = sb.get_current_url()
        if "/server/index" in current_url:
            return "already_logged"

    for wait_round in range(3):
        try:
            sb.wait_for_element_visible('#loginformmodel-username', timeout=10)
            logger.info("✅ 找到登录表单")
            return "need_login"
        except TimeoutException:
            logger.info(f"等待表单超时 ({wait_round+1}/3)")
            if is_cloudflare_interstitial(sb):
                bypass_cloudflare_interstitial(sb, max_attempts=2)
                time.sleep(3)
            else:
                time.sleep(3)

    safe_screenshot(sb, screenshot_path(account_index, "02-no-form"), result)
    logger.error("未找到登录表单")
    return None

def fill_and_submit(sb, email: str, password: str, account_index: int, result: Dict) -> bool:
    logger.info("填写登录信息...")
    sb.type('#loginformmodel-username', email)
    sb.type('#loginformmodel-password', password)
    safe_screenshot(sb, screenshot_path(account_index, "03-form-filled"), result)

    # ---------- 处理登录 Turnstile ----------
    max_turnstile_retries = 2
    turnstile_ok = False
    for retry in range(max_turnstile_retries):
        if handle_login_turnstile(sb, account_index, result):
            turnstile_ok = True
            break
        logger.warning(f"登录 Turnstile 失败（第 {retry+1}/{max_turnstile_retries} 次）")
        if retry < max_turnstile_retries - 1:
            logger.info("刷新页面重试 Turnstile …")
            sb.refresh()
            time.sleep(4)
            # 重新填写表单
            sb.type('#loginformmodel-username', email)
            sb.type('#loginformmodel-password', password)
            safe_screenshot(sb, screenshot_path(account_index, "03-form-refilled"), result)
        else:
            logger.error("登录 Turnstile 连续失败，放弃本次登录")
            return False

    logger.info("提交登录...")
    try:
        sb.click('button[type="submit"].btn-register')
    except:
        try:
            sb.execute_script('document.querySelector("form").submit()')
        except:
            logger.error("提交失败")
            return False

    time.sleep(6)
    current_url = sb.get_current_url()
    logger.info(f"登录后URL: {mask_url(current_url)}")

    if "/user/login" in current_url:
        try:
            err = sb.execute_script('''
                var alert = document.querySelector('.alert-danger, .error-message');
                return alert ? alert.innerText : '';
            ''')
            if err:
                logger.error(f"登录错误: {err}")
        except:
            pass
        safe_screenshot(sb, screenshot_path(account_index, "05-login-failed"), result)
        return False

    logger.info("✅ 登录成功")
    return True

def close_welcome_popup(sb, account_index: int, result: Dict):
    try:
        sb.wait_for_element_visible('.stpd_cmp_form', timeout=5)
        logger.info("发现隐私弹窗，尝试关闭...")
        sb.click('button.stpd_cta_btn', timeout=3)
        time.sleep(1)
        safe_screenshot(sb, screenshot_path(account_index, "06-popup-closed"), result)
    except:
        pass

# ================== 获取服务器列表 ==================
def get_all_servers(sb, account_index: int, result: Dict) -> List[Tuple[str, str]]:
    logger.info("获取服务器列表...")

    enable_cdp_adblock_interception(sb)
    sb.open(SERVER_INDEX_URL)
    time.sleep(3)

    handle_adblocker(sb, account_index, result, "server-list")
    safe_screenshot(sb, screenshot_path(account_index, "07-server-index"), result)
    close_welcome_popup(sb, account_index, result)

    last_height = sb.execute_script("return document.body.scrollHeight")
    for _ in range(5):
        sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        handle_adblocker(sb, account_index, result, "scroll")
        new_height = sb.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    time.sleep(2)
    safe_screenshot(sb, screenshot_path(account_index, "07-server-index-scrolled"), result)

    servers = sb.execute_script('''
        (function() {
            var servers = [];
            var cards = document.querySelectorAll('div.server-card');
            cards.forEach(function(card) {
                var titleEl = card.querySelector('h5.server-card-title');
                var manageLink = card.querySelector('a.btn-success');
                if (titleEl && manageLink) {
                    var name = titleEl.innerText.trim();
                    var href = manageLink.getAttribute('href');
                    var match = href.match(/\\/server\\/(\\d+)/);
                    if (match) {
                        servers.push({ id: match[1], name: name });
                    }
                }
            });
            return servers;
        })();
    ''')

    if servers is None:
        servers = []

    logger.info(f"找到 {len(servers)} 个服务器")
    for s in servers:
        logger.info(f"服务器: {mask_server_name(s['name'], s['id'])}")
    return [(s['id'], s['name']) for s in servers]

# ================== 续订单个服务器（先续订，成功后启动）==================
def renew_server(sb, server_id: str, server_name: str, account_index: int, result: Dict) -> Dict[str, Any]:
    masked_name = mask_server_name(server_name, server_id)
    logger.info(f"处理服务器 {masked_name}")

    server_result = {
        "id": server_id,
        "name": server_name,
        "success": False,
        "before": "",
        "after": "",
        "started": False
    }

    # 获取续订前到期时间
    before_expiry = get_server_expiry(sb, server_id)
    if before_expiry:
        server_result["before"] = before_expiry
        logger.info(f"续订前到期: {before_expiry}")

    renew_url = f"{BASE_URL}/server/{server_id}/renew-basic"

    # ── 外层重试循环：Turnstile 失败后重新加载页面 ──────────────────────
    MAX_RENEW_RETRIES = 3
    for renew_attempt in range(1, MAX_RENEW_RETRIES + 1):
        logger.info(f"访问续订页面（第 {renew_attempt}/{MAX_RENEW_RETRIES} 次）: {mask_url(renew_url)}")

        # 续订页面不启用 CDP 拦截（避免干扰 Turnstile）
        sb.uc_open_with_reconnect(renew_url, reconnect_time=8)
        time.sleep(5)

        # 滚动到验证组件
        sb.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(2)
        sb.execute_script('''
            (function() {
                var t = document.querySelector('.cf-turnstile');
                var b = document.querySelector('#renew-btn');
                if (t) t.scrollIntoView({behavior:'smooth', block:'center'});
                else if (b) b.scrollIntoView({behavior:'smooth', block:'center'});
                else window.scrollTo(0, document.body.scrollHeight / 2);
            })();
        ''')
        time.sleep(3)

        safe_screenshot(
            sb,
            screenshot_path(account_index,
                             f"08-renew-page-{mask_server_id(server_id)}-a{renew_attempt}"),
            result
        )

        # 等待页面关键元素就绪
        page_ready = False
        for _ in range(10):
            page_ready = sb.execute_script('''
                (function() {
                    return !!(document.querySelector('#renew-btn') ||
                             document.querySelector('.cf-turnstile') ||
                             document.querySelector('#captcha-warning'));
                })();
            ''')
            if page_ready:
                break
            time.sleep(1)

        if not page_ready:
            logger.error(f"续订页面未加载（第 {renew_attempt} 次），"
                         + ("重试…" if renew_attempt < MAX_RENEW_RETRIES else "放弃。"))
            continue

        # ── Turnstile（内部已含刷新重试，这里 max_page_retries=1 避免双重刷新）
        turnstile_ok = handle_turnstile_verification(
            sb, account_index, result,
            server_id=server_id,
            page_url=renew_url,
            max_page_retries=1          # 外层循环负责跨页重试，内层只做一轮
        )

        if not turnstile_ok:
            logger.warning(
                f"⚠️ Turnstile 验证失败（续订第 {renew_attempt}/{MAX_RENEW_RETRIES} 次）"
                + ("，刷新重试…" if renew_attempt < MAX_RENEW_RETRIES else "，放弃。")
            )
            time.sleep(3)
            continue  # 外层循环重新 uc_open_with_reconnect

        # ── 点击续订按钮 ────────────────────────────────────────────────
        try:
            sb.execute_script('''
                (function() {
                    var btn = document.querySelector('#renew-btn');
                    if (btn) btn.scrollIntoView({behavior:'smooth', block:'center'});
                })();
            ''')
            time.sleep(1)

            btn_enabled = False
            for i in range(15):
                btn_enabled = sb.execute_script('''
                    (function() {
                        var btn = document.querySelector('#renew-btn');
                        return btn && !btn.disabled;
                    })();
                ''')
                if btn_enabled:
                    logger.info(f"续订按钮已启用（等待 {i+1} 秒）")
                    break
                time.sleep(1)

            if not btn_enabled:
                logger.error("续订按钮未启用，跳过本次尝试")
                continue

            clicked = sb.execute_script('''
                (function() {
                    var btn = document.querySelector('#renew-btn');
                    if (btn && !btn.disabled) { btn.click(); return true; }
                    return false;
                })();
            ''')

            if not clicked:
                try:
                    renew_btn = sb.find_element("#renew-btn")
                    if renew_btn and renew_btn.is_enabled():
                        renew_btn.click()
                        logger.info("✅ 点击续订按钮（SeleniumBase）")
                    else:
                        continue
                except Exception as e:
                    logger.error(f"点击失败: {e}")
                    continue
            else:
                logger.info("✅ 点击续订按钮（JavaScript）")

        except Exception as e:
            logger.error(f"无法点击续订按钮: {e}")
            continue

        time.sleep(5)
        safe_screenshot(
            sb,
            screenshot_path(account_index,
                             f"10-renew-after-click-{mask_server_id(server_id)}-a{renew_attempt}"),
            result
        )

        # ── 检查续订成功弹窗 ────────────────────────────────────────────
        try:
            sb.wait_for_element_visible('.swal2-icon-success', timeout=10)
            success_text = sb.execute_script('''
                (function() {
                    var el = document.querySelector('#swal2-html-container');
                    return el ? el.innerText : '';
                })();
            ''')
            if "renewed" in success_text.lower():
                server_result["success"] = True
                logger.info(f"✅ 服务器 {masked_name} 续订成功（第 {renew_attempt} 次）")
            safe_screenshot(
                sb,
                screenshot_path(account_index,
                                 f"11-renew-success-{mask_server_id(server_id)}-a{renew_attempt}"),
                result
            )
            try:
                sb.click('.swal2-confirm')
            except:
                pass
        except:
            pass

        if server_result["success"]:
            break  # 续订成功，退出重试循环

        logger.warning(
            f"未检测到续订成功弹窗（第 {renew_attempt}/{MAX_RENEW_RETRIES} 次）"
            + ("，重试…" if renew_attempt < MAX_RENEW_RETRIES else "，放弃。")
        )
        time.sleep(3)
    # ── 重试循环结束 ────────────────────────────────────────────────────

    # 获取续订后到期时间 & 启动服务器
    if server_result["success"]:
        after_expiry = get_server_expiry(sb, server_id)
        if after_expiry:
            server_result["after"] = after_expiry
            logger.info(f"续订后到期: {after_expiry}")

        logger.info("续订成功，检查服务器状态并启动...")
        started = check_and_start_server(sb, server_id)
        if started:
            server_result["started"] = True

    return server_result

# ================== 主流程 ==================
def process_account(account_index: int, email: str, password: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    result = {"success": False, "message": "", "screenshots": [], "server_results": []}
    masked = mask_email(email)

    logger.info("=" * 50)
    logger.info(f"处理账号 {account_index}: {masked}")
    logger.info("=" * 50)

    sb_kwargs = {
        "uc": True,
        "test": True,
        "locale": "en",
        "headed": not is_linux(),
        "chromium_arg": "--disable-blink-features=AutomationControlled",
    }
    if proxy:
        sb_kwargs["proxy"] = proxy

    try:
        with SB(**sb_kwargs) as sb:
            status = handle_initial_page(sb, account_index, result)
            if status is None:
                result["message"] = "Cloudflare 绕过失败或 AdBlocker 拦截"
                return result

            if status == "need_login":
                if not fill_and_submit(sb, email, password, account_index, result):
                    result["message"] = "登录失败"
                    return result

            close_welcome_popup(sb, account_index, result)
            handle_adblocker(sb, account_index, result, "after-login")

            servers = get_all_servers(sb, account_index, result)

            if not servers:
                result["message"] = "没有找到服务器，跳过续订"
                result["success"] = True
                logger.info("⚠️ 没有服务器，跳过")
                return result

            server_results = []
            renewed = 0
            for sid, sname in servers:
                handle_adblocker(sb, account_index, result, f"before-renew-{sid}")
                sr = renew_server(sb, sid, sname, account_index, result)
                server_results.append(sr)
                if sr["success"]:
                    renewed += 1
                time.sleep(3)

            result["server_results"] = server_results

            if renewed > 0:
                result["success"] = True
                result["message"] = f"续订成功 {renewed}/{len(servers)} 个服务器"
                logger.info(f"✅ 续订成功: {renewed}/{len(servers)}")
            else:
                result["success"] = False
                result["message"] = f"所有服务器续订失败（共 {len(servers)} 个）"
                logger.error(f"❌ 所有服务器续订失败")

            return result
    except Exception as e:
        logger.exception(f"账号处理异常: {e}")
        result["message"] = str(e)
        return result

def main():
    accounts = parse_accounts()
    if not accounts:
        logger.error("未找到账号配置")
        sys.exit(1)

    proxy = os.environ.get("PROXY_SERVER") or os.environ.get("HY2_URL")
    if proxy and not proxy.startswith("http"):
        logger.warning("代理配置无效")
        proxy = None

    display = setup_display()
    success_count = 0

    try:
        for i, (email, pwd) in enumerate(accounts, 1):
            result = process_account(i, email, pwd, proxy)
            if result is None:
                result = {"success": False, "message": "未知错误", "screenshots": [], "server_results": []}

            if result["success"]:
                success_count += 1

            last_screenshot = result["screenshots"][-1] if result.get("screenshots") else None
            notify_telegram(
                i,
                email,
                result.get("server_results", []),
                result["success"],
                result.get("message", ""),
                last_screenshot
            )

            if i < len(accounts):
                logger.info("等待 15 秒...")
                time.sleep(15)

        logger.info(f"完成: {success_count}/{len(accounts)} 个账号成功")
        sys.exit(0 if success_count == len(accounts) else 1)
    finally:
        if display:
            display.stop()

if __name__ == "__main__":
    main()
