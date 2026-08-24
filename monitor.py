#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import gzip
import html
from html.parser import HTMLParser
import http.cookiejar
import json
import os
from pathlib import Path
import random
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"
STATE_FILE = BASE_DIR / "state.json"

CART_TEXT = "カートに入れる"
OUT_MARKERS = ("SOLD OUT", "在庫なし", "在庫切れ", "品切れ", "販売終了", "入荷待ち")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25
BETWEEN_PRODUCTS_MIN = 1.2
BETWEEN_PRODUCTS_MAX = 2.2

COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))


class SiteThrottleError(RuntimeError):
    pass


def now_jst() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def timestamp() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S JST")


def log(message: str) -> None:
    print(f"[{timestamp()}] {message}", flush=True)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        log(f"WARN failed to read {path.name}: {exc}")
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class CartButtonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cart_found = False
        self._button_stack: list[dict[str, Any]] = []
        self.page_text_parts: list[str] = []

    @staticmethod
    def _attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {k.lower(): (v or "") for k, v in attrs}

    @staticmethod
    def _disabled(attrs: dict[str, str]) -> bool:
        classes = attrs.get("class", "").lower()
        aria_disabled = attrs.get("aria-disabled", "").lower()
        return (
            "disabled" in attrs
            or aria_disabled == "true"
            or "disabled" in classes.split()
            or "is-disabled" in classes
        )

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = self._attrs_dict(attrs_list)
        attr_blob = " ".join(
            attrs.get(k, "") for k in ("value", "alt", "title", "aria-label", "data-label")
        )
        if tag == "input" and CART_TEXT in attr_blob and not self._disabled(attrs):
            self.cart_found = True
        elif tag in ("button", "a"):
            self._button_stack.append({"tag": tag, "attrs": attrs, "text": [attr_blob]})
            if CART_TEXT in attr_blob and not self._disabled(attrs):
                self.cart_found = True

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.page_text_parts.append(data)
        if self._button_stack:
            self._button_stack[-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in ("button", "a") or not self._button_stack:
            return
        for i in range(len(self._button_stack) - 1, -1, -1):
            item = self._button_stack[i]
            if item["tag"] == tag:
                self._button_stack.pop(i)
                text = " ".join(item["text"])
                if CART_TEXT in text and not self._disabled(item["attrs"]):
                    self.cart_found = True
                break

    @property
    def page_text(self) -> str:
        return " ".join(self.page_text_parts)


def decode_body(resp: Any, raw: bytes) -> str:
    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
            "Accept-Encoding": "gzip",
            "Cache-Control": "no-cache",
            "Connection": "close",
        },
    )
    try:
        with OPENER.open(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            return decode_body(resp, raw)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise SiteThrottleError(f"HTTP {exc.code}") from exc
        raise


def detect_stock(page_html: str) -> tuple[str, str]:
    parser = CartButtonParser()
    parser.feed(page_html)
    if parser.cart_found:
        return "in_stock", "enabled カートに入れる element found"

    visible = html.unescape(parser.page_text)
    full = html.unescape(page_html)
    for marker in OUT_MARKERS:
        if marker.lower() in visible.lower() or marker.lower() in full.lower():
            return "out_of_stock", f"marker found: {marker}"
    return "unknown", "no enabled cart element and no out-of-stock marker"


def check_product(product: dict[str, str]) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            page = fetch_html(product["url"])
            status, evidence = detect_stock(page)
            return {**product, "status": status, "evidence": evidence, "error": None}
        except SiteThrottleError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2.5)
    return {
        **product,
        "status": "unknown",
        "evidence": "request failed after retry",
        "error": f"{type(last_error).__name__}: {last_error}",
    }


def secret(name: str) -> str:
    return os.environ.get(name, "").strip()


def send_telegram(text: str) -> None:
    token = secret("TELEGRAM_BOT_TOKEN")
    chat_id = secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID is not configured")
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 400 or '"ok":true' not in body.replace(" ", ""):
            raise RuntimeError(f"Telegram API error: HTTP {resp.status}: {body[:300]}")


def send_email(subject: str, body: str) -> None:
    address = secret("EMAIL_ADDRESS")
    password = secret("EMAIL_APP_PASSWORD")
    if not address or not password:
        raise RuntimeError("EMAIL_ADDRESS / EMAIL_APP_PASSWORD is not configured")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = address
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.126.com", 465, timeout=25, context=context) as smtp:
        smtp.login(address, password)
        smtp.send_message(msg)


def notification_text(product: dict[str, str]) -> str:
    return (
        "📸 FUJIFILM 相纸到货提醒\n\n"
        f"{product['name']}\n"
        "✅ 已检测到「カートに入れる」按钮\n"
        f"检测时间：{timestamp()}\n\n"
        f"购买页面：{product['url']}"
    )


def deliver_restock(product: dict[str, str], send_tg: bool, send_mail: bool) -> tuple[bool, bool]:
    text = notification_text(product)
    tg_ok = not send_tg
    mail_ok = not send_mail
    if send_tg:
        try:
            send_telegram(text)
            tg_ok = True
        except Exception as exc:
            log(f"ERROR Telegram notification failed for {product['name']}: {exc}")
    if send_mail:
        try:
            send_email(f"【FUJIFILM 到货】{product['name']}", text)
            mail_ok = True
        except Exception as exc:
            log(f"ERROR email notification failed for {product['name']}: {exc}")
    return tg_ok, mail_ok


def test_notifications() -> int:
    text = (
        "✅ FUJIFILM 云端库存监控测试成功\n\n"
        "监控商品：20 款\n"
        "检查频率：GitHub Actions 每 5 分钟\n"
        "到货条件：商品页出现「カートに入れる」按钮\n"
        f"测试时间：{timestamp()}"
    )
    errors: list[str] = []
    try:
        send_telegram(text)
        log("Telegram test notification sent")
    except Exception as exc:
        errors.append(f"Telegram: {exc}")
    try:
        send_email("【测试成功】FUJIFILM 云端库存监控", text)
        log("Email test notification sent")
    except Exception as exc:
        errors.append(f"Email: {exc}")
    if errors:
        for item in errors:
            log(f"ERROR {item}")
        return 1
    return 0


def monitor() -> int:
    products = load_json(PRODUCTS_FILE, [])
    if len(products) != 20:
        log(f"ERROR expected 20 products, found {len(products)}")
        return 2

    state = load_json(STATE_FILE, {"products": {}})
    if not isinstance(state, dict):
        state = {"products": {}}
    pstate = state.setdefault("products", {})
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)

    log("Starting stock check for 20 products")
    alerts = 0
    unknowns = 0
    throttled = False

    for index, product in enumerate(products):
        try:
            result = check_product(product)
        except SiteThrottleError as exc:
            log(f"WARN site throttling detected at {product['name']}: {exc}. Stop this run to avoid hammering the store.")
            throttled = True
            unknowns += len(products) - index
            break

        current = result["status"]
        old = pstate.get(product["url"], {})
        if not isinstance(old, dict):
            old = {}
        stable_old = old.get("stable_status")
        pending = old.get("pending_channels", {})
        if not isinstance(pending, dict):
            pending = {}

        label = {"in_stock": "有货", "out_of_stock": "缺货", "unknown": "未知"}[current]
        print(f"  [{label}] {product['name']}", flush=True)
        if result.get("error"):
            log(f"WARN {product['name']}: {result['error']}")

        if current == "unknown":
            unknowns += 1
        else:
            entry = dict(old)
            if stable_old is None:
                entry.update({
                    "name": product["name"],
                    "url": product["url"],
                    "stable_status": current,
                    "last_change": timestamp(),
                })
            else:
                is_new_restock = current == "in_stock" and stable_old == "out_of_stock"
                is_pending = current == "in_stock" and bool(pending)
                delivery_result: tuple[bool, bool] | None = None

                if is_new_restock or is_pending:
                    send_tg = True if is_new_restock else bool(pending.get("telegram"))
                    send_mail = True if is_new_restock else bool(pending.get("email"))
                    delivery_result = deliver_restock(product, send_tg, send_mail)
                    if is_new_restock:
                        alerts += 1
                    log(
                        f"RESTOCK delivery {product['name']} | "
                        f"Telegram={delivery_result[0]} Email={delivery_result[1]}"
                    )

                entry.update({"name": product["name"], "url": product["url"]})
                if stable_old != current:
                    entry["stable_status"] = current
                    entry["last_change"] = timestamp()

                if current == "out_of_stock":
                    entry.pop("pending_channels", None)
                elif delivery_result is not None:
                    tg_ok, mail_ok = delivery_result
                    new_pending: dict[str, bool] = {}
                    if not tg_ok:
                        new_pending["telegram"] = True
                    if not mail_ok:
                        new_pending["email"] = True
                    if new_pending:
                        entry["pending_channels"] = new_pending
                    else:
                        entry.pop("pending_channels", None)
                    if tg_ok and mail_ok:
                        entry["last_notification"] = timestamp()

            pstate[product["url"]] = entry

        if index < len(products) - 1:
            time.sleep(random.uniform(BETWEEN_PRODUCTS_MIN, BETWEEN_PRODUCTS_MAX))

    week = now_jst().strftime("%G-W%V")
    if state.get("heartbeat_week") != week:
        state["heartbeat_week"] = week

    after = json.dumps(state, ensure_ascii=False, sort_keys=True)
    if after != before:
        state["state_updated_at"] = timestamp()
        atomic_write_json(STATE_FILE, state)
        log("Persistent state changed; state.json updated")
    else:
        log("Persistent state unchanged")

    log(f"Finished: alerts={alerts}, unknown={unknowns}, throttled={throttled}")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    if mode == "monitor":
        return monitor()
    if mode == "test-notify":
        return test_notifications()
    print(f"Unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
