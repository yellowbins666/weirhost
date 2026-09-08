#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hub.weirdhost.xyz 家宽游戏机自动续期
基于 oyz8/weirdhost-login 精简重构，去 xdotool/opencv/scrot 重依赖
"""

import os
import sys
import time
import asyncio
import aiohttp
import base64
import random
import re
import json
from datetime import datetime
from urllib.parse import unquote

from seleniumbase import SB

try:
    from nacl import encoding, public
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False

sys.stdout.reconfigure(line_buffering=True)

BASE_URL = "https://hub.weirdhost.xyz/server/"
API_BASE_URL = "https://hub.weirdhost.xyz/api/client"
DOMAIN = "hub.weirdhost.xyz"
MAX_COOKIE_COUNT = 5

RENEWAL_BUTTON_SELECTORS = [
    "//button//span[contains(text(), '연장하기')]/parent::button",
    "//button[contains(text(), '연장하기')]",
    "//button//span[contains(text(), '시간추가')]/parent::button",
    "//button[contains(text(), '시간추가')]",
    "//button//span[contains(text(), '시간 추가')]/parent::button",
    "//button[contains(text(), '시간 추가')]",
]

RENEW_THRESHOLD_DAYS = float(os.environ.get("RENEW_THRESHOLD_DAYS", "2"))


def get_proxy_url():
    """按优先级读取代理配置"""
    for k in ("WEIRDHOST_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        v = os.environ.get(k, "").strip()
        if v:
            if "://" not in v:
                v = "http://" + v
            return v
    return ""


# ============================================================
#  工具函数
# ============================================================

def mask_sensitive(text, show_chars=3):
    if not text:
        return "***"
    text = str(text)
    if len(text) <= show_chars * 2:
        return "*" * len(text)
    return text[:show_chars] + "*" * (len(text) - show_chars * 2) + text[-show_chars:]


def mask_email(email):
    if not email or "@" not in email:
        return mask_sensitive(email)
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def mask_remark(remark):
    if not remark:
        return "***"
    if "@" in remark:
        return mask_email(remark)
    return mask_sensitive(remark)


def mask_server_id(server_id):
    if not server_id:
        return "***"
    if len(server_id) <= 4:
        return "*" * len(server_id)
    return server_id[:2] + "*" * (len(server_id) - 4) + server_id[-2:]


def random_delay(min_sec=0.5, max_sec=2.0):
    time.sleep(random.uniform(min_sec, max_sec))


def calculate_remaining_time(expiry_str):
    try:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
            try:
                expiry_dt = datetime.strptime(expiry_str.strip(), fmt)
                diff = expiry_dt - datetime.now()
                if diff.total_seconds() < 0:
                    return "已过期"
                days = diff.days
                hours = diff.seconds // 3600
                minutes = (diff.seconds % 3600) // 60
                parts = []
                if days > 0:
                    parts.append(f"{days}天")
                if hours > 0:
                    parts.append(f"{hours}小时")
                if minutes > 0 and days == 0:
                    parts.append(f"{minutes}分钟")
                return " ".join(parts) if parts else "不到1分钟"
            except ValueError:
                continue
        return "无法解析"
    except Exception:
        return "计算失败"


def parse_expiry_to_datetime(expiry_str):
    if not expiry_str or expiry_str == "Unknown":
        return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
        try:
            return datetime.strptime(expiry_str.strip(), fmt)
        except ValueError:
            continue
    return None


def get_remaining_days(expiry_str):
    expiry_dt = parse_expiry_to_datetime(expiry_str)
    if not expiry_dt:
        return None
    diff = expiry_dt - datetime.now()
    return diff.total_seconds() / 86400


def format_remaining_days(rd):
    if rd is None:
        return "?"
    return f"{rd:.1f}"


def parse_weirdhost_cookie(cookie_str):
    if not cookie_str:
        return (None, None)
    cookie_str = cookie_str.strip()
    if "=" in cookie_str:
        parts = cookie_str.split("=", 1)
        if len(parts) == 2:
            return (parts[0].strip(), unquote(parts[1].strip()))
    return (None, None)


def build_server_url(server_id):
    if not server_id:
        return None
    server_id = server_id.strip()
    return server_id if server_id.startswith("http") else f"{BASE_URL}{server_id}"


# ============================================================
#  账号自动检测
# ============================================================

def parse_account_config(raw_value):
    if not raw_value:
        return None
    raw_value = raw_value.strip()
    remark = ""
    cookie_str = ""
    if "-----" in raw_value:
        parts = raw_value.split("-----", 1)
        remark = parts[0].strip()
        cookie_str = parts[1].strip() if len(parts) > 1 else ""
    else:
        cookie_str = raw_value
    if not cookie_str or "=" not in cookie_str:
        return None
    cookie_name, cookie_value = parse_weirdhost_cookie(cookie_str)
    if not cookie_name or not cookie_name.startswith("remember_web"):
        return None
    return {
        "remark": remark,
        "cookie_str": cookie_str,
        "cookie_name": cookie_name,
        "cookie_value": cookie_value,
    }


def detect_accounts():
    accounts = []
    for i in range(1, MAX_COOKIE_COUNT + 1):
        env_name = f"WEIRDHOST_COOKIE_{i}"
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        config = parse_account_config(raw)
        if not config:
            print(f"[WARN] {env_name} 格式错误，跳过（正确: 备注-----remember_web_xxx=yyy）")
            continue
        remark = config["remark"] or f"账号{i}"
        print(f"[INFO] 检测到 {env_name}: {mask_remark(remark)}")
        accounts.append({
            "index": i,
            "cookie_env": env_name,
            "remark": remark,
            "cookie_str": config["cookie_str"],
            "cookie_name": config["cookie_name"],
            "cookie_value": config["cookie_value"],
        })
    if accounts:
        return accounts

    for key in ("WEIRDHOST_ACCOUNTS", "ACCOUNTS"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                continue
            for idx, item in enumerate(data):
                if isinstance(item, dict):
                    c = item.get("cookie") or item.get("cookie_str") or ""
                    remark = item.get("remark") or item.get("name") or f"账号{idx+1}"
                else:
                    c = str(item)
                    remark = f"账号{idx+1}"
                cfg = parse_account_config(c if "remember_web" in c else f"{remark}-----{c}")
                if not cfg:
                    cfg = parse_account_config(c)
                if not cfg:
                    continue
                accounts.append({
                    "index": idx + 1,
                    "cookie_env": key,
                    "remark": remark,
                    "cookie_str": cfg["cookie_str"],
                    "cookie_name": cfg["cookie_name"],
                    "cookie_value": cfg["cookie_value"],
                })
            if accounts:
                print(f"[INFO] 从 {key} 解析到 {len(accounts)} 个账号")
                return accounts
        except Exception as e:
            print(f"[WARN] {key} 解析失败: {e}")
    return accounts


# ============================================================
#  Telegram 通知
# ============================================================

async def tg_notify(message):
    token = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[INFO] 未配置 TG_BOT_TOKEN/TG_CHAT_ID，跳过通知")
        return
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
        except Exception as e:
            print(f"[ERROR] TG 发送失败: {e}")


async def tg_notify_photo(photo_path, caption=""):
    token = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id or not os.path.exists(photo_path):
        return
    async with aiohttp.ClientSession() as session:
        try:
            with open(photo_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                data.add_field("photo", f, filename=os.path.basename(photo_path))
                data.add_field("caption", caption)
                data.add_field("parse_mode", "HTML")
                await session.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data)
        except Exception as e:
            print(f"[ERROR] TG 图片发送失败: {e}")


def sync_tg_notify(message):
    try:
        asyncio.run(tg_notify(message))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(tg_notify(message))


def sync_tg_notify_photo(photo_path, caption=""):
    try:
        asyncio.run(tg_notify_photo(photo_path, caption))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(tg_notify_photo(photo_path, caption))


# ============================================================
#  GitHub Secret 回写
# ============================================================

def encrypt_secret(public_key, secret_value):
    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


async def update_github_secret(secret_name, secret_value):
    repo_token = os.environ.get("REPO_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo_token or not repository or not NACL_AVAILABLE:
        return False
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {repo_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with aiohttp.ClientSession() as session:
        try:
            pk_url = f"https://api.github.com/repos/{repository}/actions/secrets/public-key"
            async with session.get(pk_url, headers=headers) as resp:
                if resp.status != 200:
                    return False
                pk_data = await resp.json()
            encrypted_value = encrypt_secret(pk_data["key"], secret_value)
            secret_url = f"https://api.github.com/repos/{repository}/actions/secrets/{secret_name}"
            async with session.put(
                secret_url,
                headers=headers,
                json={"encrypted_value": encrypted_value, "key_id": pk_data["key_id"]},
            ) as resp:
                return resp.status in (201, 204)
        except Exception:
            return False


# ============================================================
#  浏览器内 API 调用
# ============================================================

def api_fetch_json(sb, url, xsrf_token=None):
    headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://{DOMAIN}/",
    }
    if xsrf_token:
        headers["X-XSRF-TOKEN"] = xsrf_token
    script = """
        var done = arguments[arguments.length - 1];
        fetch(arguments[0], {headers: arguments[1]})
        .then(resp => {
            if (resp.status === 401) return {_error: 'unauthorized'};
            return resp.json();
        })
        .then(data => done(data))
        .catch(err => done({_error: err.toString()}));
    """
    result = sb.driver.execute_async_script(script, url, headers)
    if isinstance(result, dict) and "_error" in result:
        print(f"[ERROR] fetch 失败: {result['_error']}")
        return None
    return result


def get_xsrf_token_from_cookies(sb):
    try:
        cookies = sb.get_cookies()
        for c in cookies:
            if c.get("name") == "XSRF-TOKEN":
                return unquote(c.get("value", ""))
    except Exception:
        pass
    return None


# ============================================================
#  Turnstile 处理
# ============================================================

def ts_exists(sb):
    try:
        return sb.execute_script("""
            return !!(
                document.querySelector('input[name="cf-turnstile-response"]') ||
                document.querySelector('.cf-turnstile') ||
                document.querySelector('iframe[src*="challenges.cloudflare.com"]')
            );
        """)
    except Exception:
        return False


def ts_solved(sb):
    try:
        return sb.execute_script("""
            var i = document.querySelector('input[name="cf-turnstile-response"]');
            return i && i.value && i.value.length > 20;
        """)
    except Exception:
        return False


def expand_turnstile(sb):
    try:
        sb.execute_script("""
            (function(){
                var ti=document.querySelector('input[name="cf-turnstile-response"]');
                if(!ti) return;
                var el=ti;
                for(var i=0;i<20;i++){
                    el=el.parentElement; if(!el) break;
                    var s=window.getComputedStyle(el);
                    if(s.overflow==='hidden') el.style.overflow='visible';
                    el.style.minWidth='max-content';
                }
                document.querySelectorAll('.cf-turnstile').forEach(function(c){
                    c.style.overflow='visible'; c.style.width='300px'; c.style.height='65px';
                });
                document.querySelectorAll('iframe').forEach(function(f){
                    if(f.src && f.src.includes('challenges.cloudflare.com')){
                        f.style.width='300px'; f.style.height='65px';
                        f.style.visibility='visible'; f.style.opacity='1';
                    }
                });
            })();
        """)
    except Exception:
        pass


def focus_turnstile_area(sb):
    try:
        sb.execute_script("""
            const sels=['.cf-turnstile','iframe[src*="challenges.cloudflare"]','input[name="cf-turnstile-response"]','label.cb-lb','.cb-lb','input[type="checkbox"]'];
            for(const s of sels){const el=document.querySelector(s); if(el){el.scrollIntoView({block:'center',inline:'center'}); return true;}}
            window.scrollTo(0, document.body.scrollHeight*0.45); return false;
        """)
        time.sleep(0.5)
    except Exception:
        pass


def is_cf_challenge_page(sb):
    try:
        src = sb.get_page_source() or ""
        if "challenges.cloudflare.com" in src or "Ray ID:" in src or "Attention Required" in src:
            return True
        title = sb.execute_script("return document.title||''") or ""
        if "Cloudflare" in title:
            return True
    except Exception:
        pass
    return False


def handle_turnstile(sb, timeout=150):
    for _ in range(5):
        if ts_exists(sb) or is_cf_challenge_page(sb):
            break
        time.sleep(1)
    else:
        return True
    print("[INFO] 检测到 Turnstile/Cloudflare 验证，尝试自动解决...")
    try:
        sb.uc_gui_handle_captcha()
        for _ in range(5):
            time.sleep(1)
            if not ts_exists(sb) and not is_cf_challenge_page(sb):
                print("[INFO] UC 自动解决成功，已跳出验证页")
                return True
            if ts_solved(sb) and not is_cf_challenge_page(sb):
                print("[INFO] Turnstile 令牌已生成")
                time.sleep(2)
                if not is_cf_challenge_page(sb):
                    return True
    except Exception as e:
        print(f"[WARN] uc_gui_handle_captcha 异常: {e}")
    print("[INFO] 进入 JS 点击兜底（最长 150s）...")
    start = time.time()
    last_action = 0
    while time.time() - start < timeout:
        if not is_cf_challenge_page(sb) and not ts_exists(sb):
            print("[INFO] 验证页已消失")
            return True
        if ts_solved(sb) and not is_cf_challenge_page(sb):
            print("[INFO] Turnstile 已通过")
            time.sleep(3)
            if not is_cf_challenge_page(sb):
                return True
        expand_turnstile(sb)
        focus_turnstile_area(sb)
        now = time.time()
        if now - last_action > 5:
            clicked = False
            try:
                iframes = sb.driver.find_elements("css selector", "iframe")
                for iframe in iframes:
                    src = iframe.get_attribute("src") or ""
                    if "cloudflare" in src or "turnstile" in src or "challenges" in src:
                        try:
                            sb.driver.switch_to.frame(iframe)
                            for sel in ["input[type='checkbox']", "label.cb-lb", ".cb-lb input", "span.cb-lb", "div.cb-lb"]:
                                try:
                                    elems = sb.driver.find_elements("css selector", sel)
                                    for elem in elems:
                                        if elem.is_displayed():
                                            sb.execute_script("arguments[0].click(); arguments[0].dispatchEvent(new MouseEvent('click',{bubbles:true}));", elem)
                                            clicked = True
                                            print(f"[INFO] iframe 内 JS 点击 {sel}")
                                            break
                                    if clicked:
                                        break
                                except Exception:
                                    pass
                            if clicked:
                                break
                        except Exception:
                            pass
                        finally:
                            try:
                                sb.driver.switch_to.default_content()
                            except Exception:
                                pass
                        if clicked:
                            break
            except Exception:
                pass
            finally:
                try:
                    sb.driver.switch_to.default_content()
                except Exception:
                    pass

            if not clicked:
                try:
                    sb.execute_script("""
                        var c=document.querySelector('input[type=\"checkbox\"]')||document.querySelector('label.cb-lb')||document.querySelector('.cb-lb');
                        if(c){c.click(); c.dispatchEvent(new MouseEvent('click',{bubbles:true}));}
                    """)
                except Exception:
                    pass

            if not clicked:
                try:
                    coords = sb.execute_script("""
                        var ifr=document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]')||document.querySelector('iframe');
                        if(!ifr) return null;
                        var r=ifr.getBoundingClientRect();
                        return {x:r.x, y:r.y, w:r.width, h:r.height};
                    """)
                    if coords and coords.get('w', 0) > 0:
                        from selenium.webdriver.common.action_chains import ActionChains
                        iframe_el = sb.driver.find_element("css selector", "iframe")
                        sb.execute_script("arguments[0].scrollIntoView({block:'center'});", iframe_el)
                        time.sleep(0.5)
                        ActionChains(sb.driver).move_to_element_with_offset(iframe_el, 30, int(coords['h'] / 2)).click().perform()
                        print(f"[INFO] 坐标点击 iframe 偏移 30,{int(coords['h']/2)}")
                        clicked = True
                except Exception as e:
                    print(f"[WARN] 坐标点击失败: {e}")

            if not clicked:
                try:
                    sb.uc_gui_click_captcha()
                    print("[INFO] 已调用 uc_gui_click_captcha 兜底")
                except Exception:
                    pass
            last_action = now
        time.sleep(2)
    print("[ERROR] Turnstile 处理超时")
    try:
        sb.save_screenshot("cf_timeout.png")
    except Exception:
        pass
    return False


# ============================================================
#  续期弹窗 Turnstile
# ============================================================

def check_turnstile_exists_popup(sb):
    try:
        return sb.execute_script("return document.querySelector('input[name=\"cf-turnstile-response\"]') !== null;")
    except Exception:
        return False


def check_turnstile_solved_popup(sb):
    try:
        return sb.execute_script("""
            var i=document.querySelector('input[name="cf-turnstile-response"]');
            return i && i.value && i.value.length > 20;
        """)
    except Exception:
        return False


EXPAND_POPUP_JS = """
(function(){
    var ti=document.querySelector('input[name="cf-turnstile-response"]');
    if(!ti) return 'no input';
    var el=ti;
    for(var i=0;i<20;i++){
        el=el.parentElement; if(!el) break;
        var s=window.getComputedStyle(el);
        if(s.overflow==='hidden'||s.overflowX==='hidden'||s.overflowY==='hidden') el.style.overflow='visible';
        el.style.minWidth='max-content';
    }
    document.querySelectorAll('[class*="sc-fKFyDc"], [class*="nwOmR"]').forEach(function(c){
        c.style.overflow='visible'; c.style.width='300px'; c.style.minWidth='300px'; c.style.height='65px';
    });
    document.querySelectorAll('iframe').forEach(function(f){
        if(f.src && f.src.includes('challenges.cloudflare.com')){
            f.style.width='300px'; f.style.height='65px'; f.style.minWidth='300px';
            f.style.visibility='visible'; f.style.opacity='1';
        }
    });
    return 'done';
})();
"""


def click_turnstile_checkbox_js(sb):
    try:
        iframes = sb.driver.find_elements("css selector", "iframe")
        for iframe in iframes:
            src = iframe.get_attribute("src") or ""
            if "cloudflare" in src or "turnstile" in src or "challenges" in src:
                try:
                    sb.driver.switch_to.frame(iframe)
                    for sel in ["input[type='checkbox']", "label.cb-lb", ".cb-lb", "span"]:
                        try:
                            elems = sb.driver.find_elements("css selector", sel)
                            for e in elems:
                                if e.is_displayed():
                                    sb.execute_script("arguments[0].click(); arguments[0].dispatchEvent(new MouseEvent('click',{bubbles:true}));", e)
                                    print(f"[INFO] JS 点击 iframe 内 {sel}")
                                    sb.driver.switch_to.default_content()
                                    return True
                        except Exception:
                            continue
                    sb.driver.switch_to.default_content()
                except Exception:
                    try:
                        sb.driver.switch_to.default_content()
                    except Exception:
                        pass
        clicked = sb.execute_script("""
            var sels=['input[type="checkbox"]','label.cb-lb','.cb-lb'];
            for(var i=0;i<sels.length;i++){
                var el=document.querySelector(sels[i]);
                if(el){ el.click(); el.dispatchEvent(new MouseEvent('click',{bubbles:true})); return true; }
            }
            return false;
        """)
        if clicked:
            print("[INFO] 主文档 JS 点击成功")
            return True
        try:
            sb.uc_gui_click_captcha()
            return True
        except Exception:
            return False
    except Exception as e:
        print(f"[WARN] JS 点击失败: {e}")
        return False


def check_result_popup(sb):
    try:
        return sb.execute_script("""
            var btns=document.querySelectorAll('button');
            var hasNext=false;
            for(var i=0;i<btns.length;i++){ if(btns[i].innerText.includes('NEXT')||btns[i].innerText.includes('Next')){hasNext=true; break;}}
            var txt=document.body.innerText||'';
            var hasSuccessTitle=txt.includes('Success');
            var hasSuccessContent=txt.includes('성공')||txt.includes('갱신')||txt.includes('연장');
            var hasCooldown=txt.includes('아직')||txt.includes('Error');
            if(hasNext||hasSuccessTitle){
                if(hasCooldown && txt.includes('아직')) return 'cooldown';
                if(hasSuccessTitle && hasSuccessContent) return 'success';
                if(hasNext){ if(hasCooldown) return 'cooldown'; if(hasSuccessContent) return 'success';}
            }
            return null;
        """)
    except Exception:
        return None


def check_popup_still_open(sb):
    try:
        return sb.execute_script("""
            var t=document.querySelector('input[name="cf-turnstile-response"]');
            if(!t) return false;
            var btns=document.querySelectorAll('button');
            for(var i=0;i<btns.length;i++){
                var text=btns[i].innerText||'';
                if((text.includes('시간추가')||text.includes('시간 추가')||text.includes('연장하기')) && !text.includes('DELETE')){
                    var r=btns[i].getBoundingClientRect();
                    if(r.x>200 && r.width>0) return true;
                }
            }
            return false;
        """)
    except Exception:
        return False


def click_next_button(sb):
    try:
        for sel in ["//button[contains(text(), 'NEXT')]", "//button[contains(text(), 'Next')]", "//button//span[contains(text(), 'NEXT')]"]:
            if sb.is_element_visible(sel):
                sb.click(sel)
                print("[INFO] 已点击 NEXT")
                return True
    except Exception:
        pass
    return False


def handle_renewal_popup(sb, screenshot_prefix="", timeout=90):
    screenshot_name = f"{screenshot_prefix}_popup.png" if screenshot_prefix else "popup_fixed.png"
    print("[INFO] [阶段1] 等待弹窗和 Turnstile...")
    turnstile_ready = False
    for _ in range(20):
        result = check_result_popup(sb)
        if result == "cooldown":
            print("[INFO] 检测到冷却期")
            sb.save_screenshot(screenshot_name)
            return {"status": "cooldown", "screenshot": screenshot_name}
        if result == "success":
            print("[INFO] 检测到成功")
            sb.save_screenshot(screenshot_name)
            return {"status": "success", "screenshot": screenshot_name}
        if check_turnstile_exists_popup(sb):
            turnstile_ready = True
            print("[INFO] 检测到 Turnstile")
            break
        time.sleep(1)
    if not turnstile_ready:
        print("[WARN] 未检测到 Turnstile")
        sb.save_screenshot(screenshot_name)
        return {"status": "error", "message": "未检测到 Turnstile", "screenshot": screenshot_name}

    print("[INFO] [阶段2] 修复弹窗样式...")
    for _ in range(3):
        sb.execute_script(EXPAND_POPUP_JS)
        time.sleep(0.5)
    sb.save_screenshot(screenshot_name)

    print("[INFO] [阶段3] 点击 Turnstile...")
    for attempt in range(6):
        if check_turnstile_solved_popup(sb):
            print("[INFO] Turnstile 已通过!")
            break
        sb.execute_script(EXPAND_POPUP_JS)
        time.sleep(0.3)
        click_turnstile_checkbox_js(sb)
        for _ in range(8):
            time.sleep(0.5)
            if check_turnstile_solved_popup(sb):
                print("[INFO] Turnstile 已通过!")
                break
        if check_turnstile_solved_popup(sb):
            break
        sb.save_screenshot(f"{screenshot_prefix}_turnstile_{attempt}.png" if screenshot_prefix else f"turnstile_attempt_{attempt}.png")

    print("[INFO] 等待提交结果...")
    result_start = time.time()
    last_screenshot_time = 0
    while time.time() - result_start < 45:
        result = check_result_popup(sb)
        if result == "success":
            print("[INFO] 续期成功!")
            sb.save_screenshot(screenshot_name)
            time.sleep(1)
            click_next_button(sb)
            return {"status": "success", "screenshot": screenshot_name}
        if result == "cooldown":
            print("[INFO] 冷却期内")
            sb.save_screenshot(screenshot_name)
            time.sleep(1)
            click_next_button(sb)
            return {"status": "cooldown", "screenshot": screenshot_name}
        if not check_popup_still_open(sb):
            time.sleep(2)
            result = check_result_popup(sb)
            if result:
                sb.save_screenshot(screenshot_name)
                if result == "success":
                    click_next_button(sb)
                    return {"status": "success", "screenshot": screenshot_name}
                elif result == "cooldown":
                    click_next_button(sb)
                    return {"status": "cooldown", "screenshot": screenshot_name}
        if time.time() - last_screenshot_time > 5:
            sb.save_screenshot(screenshot_name)
            last_screenshot_time = time.time()
        time.sleep(1)
    print("[WARN] 等待结果超时")
    sb.save_screenshot(screenshot_name)
    return {"status": "timeout", "screenshot": screenshot_name}


# ============================================================
#  页面交互
# ============================================================

def get_expiry_from_page(sb):
    try:
        page_text = sb.get_page_source()
        m = re.search(r'유통기한\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', page_text)
        if m:
            return m.group(1).strip()
        m = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', page_text)
        if m:
            return m.group(1).strip()
        return "Unknown"
    except Exception:
        return "Unknown"


def find_renewal_button(sb):
    for selector in RENEWAL_BUTTON_SELECTORS:
        try:
            if sb.is_element_present(selector):
                return selector
        except Exception:
            continue
    return None


def check_renewal_button_enabled(sb):
    xpath = find_renewal_button(sb)
    if not xpath:
        return (False, False, None, "页面上未找到续期按钮")
    try:
        is_disabled = sb.execute_script(f"""
            var btn=document.evaluate("{xpath}", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if(!btn) return null;
            return btn.disabled || btn.getAttribute('aria-disabled')==='true' || btn.classList.contains('disabled');
        """)
        if is_disabled is None:
            return (False, False, None, "按钮元素无法访问")
        if is_disabled:
            return (True, False, xpath, "续期按钮已禁用（可能在冷却期）")
    except Exception:
        pass
    return (True, True, xpath, "")


def is_logged_in(sb):
    try:
        url = sb.get_current_url()
        if "/login" in url or "/auth" in url:
            return False
        if get_expiry_from_page(sb) != "Unknown":
            return True
        if find_renewal_button(sb):
            return True
        if sb.is_element_present("//div[contains(@class,'ServerControls')]") or sb.is_element_present("//a[contains(@href,'/server/')]"):
            return True
        return False
    except Exception:
        return False


def check_and_update_cookie(sb, cookie_env, original_cookie_value, remark=""):
    try:
        cookies = sb.get_cookies()
        for cookie in cookies:
            if cookie.get("name", "").startswith("remember_web"):
                new_val = cookie.get("value", "")
                c_name = cookie.get("name", "")
                if new_val and new_val != original_cookie_value:
                    new_cookie_str = f"{c_name}={new_val}"
                    new_secret_value = f"{remark}-----{new_cookie_str}" if remark else new_cookie_str
                    print(f"[INFO] Cookie 已变化，更新 {cookie_env}...")
                    if asyncio.run(update_github_secret(cookie_env, new_secret_value)):
                        print(f"[INFO] {cookie_env} 已更新")
                        return True
                    else:
                        print(f"[ERROR] {cookie_env} 更新失败（需 REPO_TOKEN）")
                        return False
                break
    except Exception as e:
        print(f"[ERROR] Cookie 检查失败: {e}")
    return False


# ============================================================
#  单服 / 单账号
# ============================================================

def process_single_server(sb, server_info, cookie_name, cookie_value, cookie_env, remark, screenshot_prefix):
    server_id = server_info.get("identifier", "Unknown")
    server_uuid = server_info.get("uuid", "")
    server_type = server_info.get("server_type", "notfree")
    server_name = server_info.get("name", "")
    api_expiry = server_info.get("expire", "Unknown")
    srv_result = {
        "server_id": server_id, "server_uuid": server_uuid, "server_type": server_type,
        "server_name": server_name, "status": "unknown", "original_expiry": api_expiry,
        "new_expiry": api_expiry, "message": "", "screenshot": None, "cookie_updated": False,
    }
    server_url = build_server_url(server_id)
    rd = get_remaining_days(api_expiry)
    dd = format_remaining_days(rd)
    print(f"\n  ──────────────────────────────────────────")
    print(f"  [INFO] 服务器: {mask_server_id(server_id)} [{server_type}] {server_name}")
    print(f"  [INFO] 到期: {api_expiry} | 剩余: {calculate_remaining_time(api_expiry)} ({dd}天)")
    print(f"  [INFO] 访问服务器页面...")
    try:
        sb.uc_open_with_reconnect(server_url, reconnect_time=5)
        time.sleep(3)
        if not is_logged_in(sb):
            sb.add_cookie({"name": cookie_name, "value": cookie_value, "domain": DOMAIN, "path": "/"})
            sb.uc_open_with_reconnect(server_url, reconnect_time=5)
            time.sleep(3)
        if not is_logged_in(sb):
            ss_path = f"{screenshot_prefix}_login_fail.png"
            sb.save_screenshot(ss_path)
            srv_result.update(status="error", message="浏览器登录失败", screenshot=ss_path)
            print(f"  [ERROR] 浏览器登录失败")
            return srv_result
        print(f"  [INFO] 登录成功")
        page_expiry = get_expiry_from_page(sb)
        if page_expiry != "Unknown":
            srv_result["original_expiry"] = page_expiry
        print(f"  [INFO] 检查续期按钮...")
        btn_found, btn_enabled, btn_xpath, btn_reason = check_renewal_button_enabled(sb)
        if not btn_found:
            print(f"  [WARN] {btn_reason}")
            ss_path = f"{screenshot_prefix}_no_btn.png"
            sb.save_screenshot(ss_path)
            srv_result.update(status="skipped", message=btn_reason, screenshot=ss_path)
            return srv_result
        if not btn_enabled:
            print(f"  [WARN] {btn_reason}")
            ss_path = f"{screenshot_prefix}_btn_disabled.png"
            sb.save_screenshot(ss_path)
            srv_result.update(status="skipped", message=btn_reason, screenshot=ss_path)
            return srv_result
        print(f"  [INFO] 续期按钮可用，执行续期")
        random_delay(1.0, 2.0)
        sb.click(btn_xpath)
        print(f"  [INFO] 已点击续期按钮，等待弹窗...")
        time.sleep(3)
        popup_result = handle_renewal_popup(sb, screenshot_prefix=screenshot_prefix, timeout=90)
        srv_result["screenshot"] = popup_result.get("screenshot")
        time.sleep(3)
        xsrf_token = get_xsrf_token_from_cookies(sb)
        if server_uuid:
            ep = f"/freeservers/{server_uuid}/info" if server_type == "free" else f"/notfreeservers/{server_uuid}/info"
            new_info = api_fetch_json(sb, f"{API_BASE_URL}{ep}", xsrf_token)
            if new_info and new_info.get("success"):
                new_expiry = new_info.get("data", {}).get("expire", srv_result["original_expiry"])
            else:
                sb.uc_open_with_reconnect(server_url, reconnect_time=3)
                time.sleep(3)
                new_expiry = get_expiry_from_page(sb)
        else:
            sb.uc_open_with_reconnect(server_url, reconnect_time=3)
            time.sleep(3)
            new_expiry = get_expiry_from_page(sb)
        srv_result["new_expiry"] = new_expiry
        original_dt = parse_expiry_to_datetime(srv_result["original_expiry"])
        new_dt = parse_expiry_to_datetime(new_expiry)
        if popup_result["status"] == "cooldown":
            srv_result.update(status="cooldown", message="冷却期内")
            print(f"  [INFO] 冷却期内")
        elif original_dt and new_dt and new_dt > original_dt:
            diff_h = (new_dt - original_dt).total_seconds() / 3600
            srv_result.update(status="success", message=f"延长了 {diff_h:.1f} 小时")
            print(f"  [INFO] 续期成功！延长 {diff_h:.1f} 小时")
        elif popup_result["status"] == "success":
            srv_result.update(status="success", message="操作完成")
            print(f"  [INFO] 续期成功")
        else:
            srv_result.update(status=popup_result["status"], message=popup_result.get("message", "未知"))
            print(f"  [WARN] 结果: {popup_result['status']}")
        if check_and_update_cookie(sb, cookie_env, cookie_value, remark):
            srv_result["cookie_updated"] = True
        if not srv_result["screenshot"] or not os.path.exists(srv_result["screenshot"]):
            final_ss = f"{screenshot_prefix}_final.png"
            sb.save_screenshot(final_ss)
            srv_result["screenshot"] = final_ss
    except Exception as e:
        import traceback
        print(f"  [ERROR] 异常: {repr(e)}")
        traceback.print_exc()
        srv_result.update(status="error", message=str(e)[:100])
        try:
            ss_path = f"{screenshot_prefix}_error.png"
            sb.save_screenshot(ss_path)
            srv_result["screenshot"] = ss_path
        except Exception:
            pass
    return srv_result


def process_single_account(sb, account, account_index):
    remark = account.get("remark", f"账号{account_index + 1}")
    cookie_env = account.get("cookie_env", "")
    cookie_name = account.get("cookie_name", "")
    cookie_value = account.get("cookie_value", "")
    result = {"remark": remark, "cookie_env": cookie_env, "email": "Unknown", "status": "unknown", "message": "", "servers": [], "cookie_updated": False}
    print(f"\n{'='*60}")
    print(f"[INFO] 处理账号 [{account_index + 1}]: {mask_remark(remark)} ({cookie_env})")
    print(f"{'='*60}")
    print(f"[INFO] [步骤1] 访问站点并处理 Cloudflare 验证...")
    sb.uc_open_with_reconnect(f"https://{DOMAIN}/", reconnect_time=5)
    if not handle_turnstile(sb):
        print(f"[ERROR] Turnstile 验证失败")
        result["status"] = "error"
        result["message"] = "Cloudflare Turnstile 验证失败"
        return result
    print(f"[INFO] CF 验证通过")
    print(f"[INFO] [步骤2] 注入 Cookie 并登录...")
    sb.add_cookie({"name": cookie_name, "value": cookie_value, "domain": DOMAIN, "path": "/"})
    sb.uc_open_with_reconnect(f"https://{DOMAIN}/", reconnect_time=5)
    time.sleep(3)
    if not is_logged_in(sb):
        print("[WARN] 未检测到登录状态，尝试刷新...")
        sb.uc_open_with_reconnect(f"https://{DOMAIN}/server/", reconnect_time=5)
        time.sleep(3)
    if not is_logged_in(sb):
        ss_path = f"acc{account_index+1}_login_fail.png"
        sb.save_screenshot(ss_path)
        result["status"] = "cookie_invalid"
        result["message"] = "Cookie 失效或登录失败"
        return result
    xsrf_token = get_xsrf_token_from_cookies(sb)
    print(f"[INFO] 登录成功")
    print(f"[INFO] [步骤3] 获取账号信息...")
    server_data = api_fetch_json(sb, f"{API_BASE_URL}?page=1", xsrf_token)
    if not server_data or server_data.get("error") == "unauthorized":
        print(f"[ERROR] 获取服务器列表失败")
        result["status"] = "error"
        result["message"] = "无法获取服务器列表"
        return result
    email_data = api_fetch_json(sb, f"{API_BASE_URL}/account/activity?sort=-timestamp&page=1&include[]=actor", xsrf_token)
    email = None
    if email_data:
        for item in email_data.get("data", []):
            actor = item.get("attributes", {}).get("relationships", {}).get("actor", {})
            if actor.get("object") == "user":
                email = actor.get("attributes", {}).get("email")
                if email:
                    break
    result["email"] = email or "Unknown"
    servers = []
    for s in server_data.get("data", []):
        attrs = s.get("attributes", {})
        stype = attrs.get("server_type", "")
        info = {"identifier": attrs.get("identifier", ""), "uuid": attrs.get("uuid", ""), "name": attrs.get("name", ""), "server_type": stype, "expire": "Unknown", "add_hours": "Unknown"}
        if attrs.get("uuid") and stype in ("notfree", "free"):
            ep = f"/freeservers/{attrs['uuid']}/info" if stype == "free" else f"/notfreeservers/{attrs['uuid']}/info"
            si = api_fetch_json(sb, f"{API_BASE_URL}{ep}", xsrf_token)
            if si and si.get("success"):
                d = si.get("data", {})
                info["expire"] = d.get("expire", "Unknown")
                info["add_hours"] = d.get("addHours", "Unknown")
        servers.append(info)
    result["servers"] = servers
    if email and email != "Unknown":
        print(f"[INFO] 邮箱: {mask_email(email)}")
    if not servers:
        print(f"[WARN] 该账号下没有服务器")
        result["status"] = "no_server"
        result["message"] = "该账号下没有服务器"
        return result
    print(f"[INFO] 找到 {len(servers)} 个服务器:")
    for s in servers:
        print(f"  - {mask_server_id(s['identifier'])} [{s['server_type']}] {s['name']} | 到期: {s['expire']}")
    print(f"[INFO] [步骤4] 逐个处理服务器续期...")
    server_results = []
    for srv_idx, server in enumerate(servers):
        ss_prefix = f"acc{account_index + 1}_srv{srv_idx + 1}"
        srv_result = process_single_server(sb, server, cookie_name, cookie_value, cookie_env, remark, ss_prefix)
        server_results.append(srv_result)
        if srv_result.get("cookie_updated"):
            result["cookie_updated"] = True
        if srv_idx < len(servers) - 1:
            wait = random.randint(2, 4) if srv_result.get("status") == "skipped" else random.randint(5, 10)
            print(f"\n  [INFO] 等待 {wait} 秒后处理下一个服务器...")
            time.sleep(wait)
    result["servers"] = server_results
    statuses = [s["status"] for s in server_results]
    if "success" in statuses:
        result["status"] = "success"
        result["message"] = f"{statuses.count('success')}/{len(statuses)} 个服务器续期成功"
    elif all(s == "skipped" for s in statuses):
        result["status"] = "skipped"
        result["message"] = "所有服务器均跳过"
    elif "cooldown" in statuses:
        result["status"] = "cooldown"
        result["message"] = "冷却期内"
    elif "error" in statuses or "timeout" in statuses:
        result["status"] = "error"
        err_count = statuses.count("error") + statuses.count("timeout")
        result["message"] = f"{err_count}/{len(statuses)} 个服务器失败"
    else:
        result["status"] = statuses[0] if statuses else "unknown"
    return result


def send_account_notification(result):
    email = result.get("email", "Unknown")
    remark = result.get("remark", "")
    cookie_updated = result.get("cookie_updated", False)
    servers = result.get("servers", [])
    status = result.get("status", "unknown")
    account_display = email if email and email != "Unknown" else remark
    lines = [f"账号：{account_display}"]
    if status == "cookie_invalid":
        lines.append("状态：⚠️ Cookie 已失效，请及时更新 WEIRDHOST_COOKIE_*")
    elif status == "no_server":
        lines.append("状态：⚠️ 没有服务器")
    else:
        for s in servers:
            lines.append("")
            lines.append(f"服务器：{s.get('server_id', '')}")
            srv_status = s["status"]
            if srv_status == "success":
                lines.append("状态：🟢 续期成功")
                new_exp = s.get("new_expiry", "Unknown")
                lines.append(f"剩余：{calculate_remaining_time(new_exp)}")
                msg = s.get("message", "")
                if msg and "延长" in msg:
                    lines.append(f"延长：{msg}")
                else:
                    orig = s.get("original_expiry", "Unknown")
                    new = s.get("new_expiry", "Unknown")
                    if orig != "Unknown" and new != "Unknown":
                        odt = parse_expiry_to_datetime(orig)
                        ndt = parse_expiry_to_datetime(new)
                        if odt and ndt and ndt > odt:
                            diff_h = (ndt - odt).total_seconds() / 3600
                            lines.append(f"延长：延长{diff_h:.1f}h")
            elif srv_status == "cooldown":
                lines.append("状态：⏳ 冷却期")
                expiry = s.get("original_expiry", "Unknown")
                lines.append(f"剩余：{calculate_remaining_time(expiry)}")
            elif srv_status == "skipped":
                lines.append("状态：⏭️ 跳过")
                expiry = s.get("original_expiry", s.get("new_expiry", "Unknown"))
                lines.append(f"剩余：{calculate_remaining_time(expiry)}")
                lines.append(f"原因：{s.get('message', '未知')}")
            else:
                lines.append(f"状态：❌ {srv_status}")
                lines.append(f"信息：{s.get('message', '未知')}")
    if cookie_updated:
        lines.append("")
        lines.append("🔑 Cookie 已自动更新")
    lines.append("")
    lines.append("Weirdhost Auto Renew")
    message = "\n".join(lines)
    screenshot = None
    for s in servers:
        if s["status"] in ("success", "cooldown", "error", "timeout"):
            if s.get("screenshot") and os.path.exists(s["screenshot"]):
                screenshot = s["screenshot"]
                break
    if screenshot:
        sync_tg_notify_photo(screenshot, message)
    else:
        sync_tg_notify(message)


def add_server_time():
    accounts = detect_accounts()
    if not accounts:
        print("\n" + "="*60)
        print("[ERROR] 未检测到任何有效的账号配置")
        print("="*60)
        print("\n请在 GitHub Secrets 中设置 WEIRDHOST_COOKIE_1 ~ WEIRDHOST_COOKIE_5")
        print("\n格式: 备注-----remember_web_xxx=yyy")
        print("="*60)
        sync_tg_notify("🔔 <b>Weirdhost 续期</b>\n\n❌ 未检测到任何有效的 WEIRDHOST_COOKIE_N\n\n请在 GitHub Secrets 中设置:\n<code>WEIRDHOST_COOKIE_1</code>\n格式: <code>备注-----remember_web_xxx=yyy</code>")
        return
    print("="*60)
    print(f"[INFO] Weirdhost 自动续期")
    print(f"[INFO] 共 {len(accounts)} 个账号 | 阈值 {RENEW_THRESHOLD_DAYS} 天")
    print("="*60)
    results = []

    # 1. 临时清理污染 Python 与本地 ChromeDriver 通信的环境变量
    proxy_url = get_proxy_url()
    proxy_keys = ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]
    saved_proxies = {}
    for key in proxy_keys:
        if key in os.environ:
            saved_proxies[key] = os.environ.pop(key)

    os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"

    try:
        chromium_arg = "--disable-dev-shm-usage,--no-sandbox"

        sb_kwargs = {
            "uc": True,
            "test": True,
            "locale": "ko",
            "headless": False,
            "chromium_arg": chromium_arg,
        }

        # 2. 如果存在代理，通过 SeleniumBase 原生 proxy 参数安全传给 Chrome
        if proxy_url:
            sb_kwargs["proxy"] = proxy_url
            print(f"[INFO] 已启用代理模式: {proxy_url.split('@')[-1][:30]}***")
        else:
            print("[INFO] 未配置代理，直连运行")

        with SB(**sb_kwargs) as sb:
            print("\n[INFO] 浏览器已启动")
            for i, account in enumerate(accounts):
                result = process_single_account(sb, account, i)
                results.append(result)
                send_account_notification(result)
                if i < len(accounts) - 1:
                    wait_time = random.randint(2, 4) if result.get("status") == "skipped" else random.randint(5, 10)
                    print(f"\n[INFO] 等待 {wait_time} 秒后处理下一个账号...")
                    time.sleep(wait_time)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] 浏览器异常: {repr(e)}")
        traceback.print_exc()
        if not results:
            sync_tg_notify(f"🔔 <b>Weirdhost</b>\n\n❌ 浏览器启动失败\n\n<code>{repr(e)}</code>")
        return
    finally:
        os.environ.update(saved_proxies)

    print(f"\n{'='*60}")
    print("[INFO] 全部处理完成")
    print(f"{'='*60}")
    icons = {"success": "🟢", "cooldown": "🟡", "skipped": "🔵", "cookie_invalid": "🔒", "no_server": "📭", "error": "❌", "timeout": "⚠️"}
    for r in results:
        icon = icons.get(r["status"], "❓")
        print(f"  {icon} {mask_remark(r.get('remark','?'))} ({mask_email(r.get('email',''))}) | {len(r.get('servers',[]))} 个服务器 | {r['status']} | {r.get('message','')}")


if __name__ == "__main__":
    if os.path.exists(".env") and not os.environ.get("WEIRDHOST_COOKIE_1"):
        try:
            with open(".env", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            print("[INFO] 已加载 .env")
        except Exception:
            pass
    add_server_time()
