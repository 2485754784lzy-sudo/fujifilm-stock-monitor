#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "fx_state.json"

BASE_THRESHOLD = 4.40       # 100 JPY = CNY
STEP = 0.05                 # alert again every +0.05 CNY
REQUEST_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"
)


def now_jst() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def timestamp() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S JST")


def log(message: str) -> None:
    print(f"[{timestamp()}] {message}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"highest_alerted_threshold": None}
    except Exception as exc:
        log(f"WARN failed to read fx_state.json: {exc}")
        return {"highest_alerted_threshold": None}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_jpy_cny() -> tuple[float, str]:
    """Return CNY per 100 JPY and source label."""
    symbol = urllib.parse.quote("JPYCNY=X", safe="")
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d",
    ]
    errors: list[str] = []

    for url in urls:
        try:
            data = fetch_json(url)
            result = data["chart"]["result"][0]
            meta = result.get("meta", {})
            price = meta.get("regularMarketPrice")

            if price is None:
                closes = (
                    result.get("indicators", {})
                    .get("quote", [{}])[0]
                    .get("close", [])
                )
                price = next((x for x in reversed(closes) if x is not None), None)

            if price is None:
                raise RuntimeError("Yahoo response did not contain a usable price")

            rate_100 = float(price) * 100.0
            if not 3.0 < rate_100 < 6.0:
                raise RuntimeError(f"implausible JPY/CNY rate: {rate_100}")

            return rate_100, "Yahoo Finance (JPYCNY=X)"
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    raise RuntimeError("all FX sources failed: " + " | ".join(errors))


def secret(name: str) -> str:
    return os.environ.get(name, "").strip()


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


def highest_reached_threshold(rate_100: float) -> float | None:
    if rate_100 + 1e-9 < BASE_THRESHOLD:
        return None
    steps = math.floor((rate_100 - BASE_THRESHOLD + 1e-9) / STEP)
    return round(BASE_THRESHOLD + steps * STEP, 2)


def alert_body(rate_100: float, threshold: float, source: str) -> str:
    cny_for_120k = rate_100 * 1200
    return (
        "日元兑人民币汇率达到新的提醒档位。\n\n"
        f"当前汇率：100 日元 = {rate_100:.4f} 人民币\n"
        f"本次提醒档位：{threshold:.2f}\n"
        f"12万日元按该市场汇率约 = {cny_for_120k:,.2f} 人民币\n"
        f"检测时间：{timestamp()}\n"
        f"数据源：{source}\n\n"
        "提醒规则：首次达到 4.40 后，每再上涨 0.05（4.45、4.50、4.55……）提醒一次。\n"
        "已提醒过的档位即使跌破后再次涨回，也不会重复发送。\n\n"
        "注：这是市场参考汇率，实际银行、支付平台或换汇机构成交价可能有点差。"
    )


def run_monitor() -> None:
    rate_100, source = fetch_jpy_cny()
    log(f"JPY/CNY: 100 JPY = {rate_100:.4f} CNY")

    reached = highest_reached_threshold(rate_100)
    if reached is None:
        log("Below 4.40; no alert.")
        return

    state = load_state()
    previous = state.get("highest_alerted_threshold")
    previous_value = float(previous) if previous is not None else None

    if previous_value is not None and reached <= previous_value + 1e-9:
        log(f"Threshold {reached:.2f} already covered by previous alert {previous_value:.2f}; no email.")
        return

    subject = f"【日元汇率提醒】100日元={rate_100:.4f}元，达到{reached:.2f}档"
    body = alert_body(rate_100, reached, source)
    send_email(subject, body)
    log(f"Email sent for threshold {reached:.2f}.")

    state["highest_alerted_threshold"] = reached
    state["alerted_at"] = timestamp()
    state["rate_at_alert"] = round(rate_100, 6)
    save_state(state)


def run_test() -> None:
    rate_100, source = fetch_jpy_cny()
    subject = f"【测试成功】日元汇率监控：100日元={rate_100:.4f}元"
    body = (
        "日元兑人民币云端监控测试邮件。\n\n"
        f"当前汇率：100 日元 = {rate_100:.4f} 人民币\n"
        f"12万日元约 = {rate_100 * 1200:,.2f} 人民币\n"
        f"检测时间：{timestamp()}\n"
        f"数据源：{source}\n\n"
        "测试不会修改正式提醒档位。"
    )
    send_email(subject, body)
    log("Test email sent successfully.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    if mode == "test-email":
        run_test()
    elif mode == "monitor":
        run_monitor()
    else:
        raise SystemExit(f"Unknown mode: {mode}")
