#!/usr/bin/env python3
"""
OKX Crypto News & Black-Swan Circuit Breaker Harvester
Features:
1. Harvest high-impact crypto news from OKX (Golden Finance, BlockBeats, TechFlow, WallStreetCN)
2. Aggregate real-time multi-coin social & news sentiment (Bullish vs Bearish Ratio)
3. Detect Black-Swan / Extreme Macro Events and trigger Automatic Circuit Breaker (30-min opening freeze)
4. Push critical alerts to QQ Channel
"""

import os
import json
import time
import datetime
import subprocess
import re

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(WORKSPACE_DIR, "data")
NEWS_CACHE_FILE = os.path.join(DATA_DIR, "news_sentiment.json")
CIRCUIT_BREAKER_FILE = os.path.join(DATA_DIR, "circuit_breaker.json")
from instrument_pool import load_instruments
TARGET_COINS = [item["name"] for item in load_instruments()]

# Institutional-Grade Extreme Black-Swan Regular Expressions
# Only trigger circuit breaker for existential, catastrophic, systemic market shocks
BLACK_SWAN_PATTERNS = [
    (r"(USDT|USDC|DAI).*(严重脱锚|脱锚幅度|depeg|脱锚超过|跌破0\.9[0-8])", "头部稳定币恶性脱锚危机"),
    (r"(币安|OKX|Coinbase|Kraken).*(暂停全部提现|停止提币|申请破产重组|破产倒闭|发生严重挤兑)", "主流中心化交易所崩盘挤兑"),
    (r"(以太坊主网|比特币网络|Solana网络|BNB Chain).*(遭遇51%攻击|全网瘫痪停机|紧急硬分叉回滚)", "顶级底层公链系统性故障/51%攻击"),
    (r"(全面取缔所有加密|宣布比特币非法|宣布数字货币交易非法|爆发核危机|宣战)", "国家级极端不可抗力/战争")
]

def run_json_cmd(cmd: str, timeout: int = 15):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if res.stdout.strip():
            return json.loads(res.stdout.strip())
    except Exception:
        pass
    return None

def trigger_circuit_breaker(headline: str, keyword: str):
    tz_bj = datetime.timezone(datetime.timedelta(hours=8))
    now_bj = datetime.datetime.now(tz_bj)
    now_ts = int(time.time())
    
    cb_data = {
        "active": True,
        "triggered_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at_ts": now_ts + 1800,  # 30 minutes freeze
        "headline": headline,
        "keyword": keyword,
        "action": "暂停新开仓 30 分钟，启动存量持仓保本防御"
    }
    
    with open(CIRCUIT_BREAKER_FILE, "w", encoding="utf-8") as f:
        json.dump(cb_data, f, ensure_ascii=False, indent=2)
        
    print(f"🚨 黑天鹅熔断已激活: {headline}")

def is_circuit_breaker_active():
    if os.path.exists(CIRCUIT_BREAKER_FILE):
        try:
            with open(CIRCUIT_BREAKER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("active") and time.time() < data.get("expires_at_ts", 0):
                    return True, data
        except Exception:
            pass
    return False, {}

def fetch_and_analyze_news_sentiment():
    tz_bj = datetime.timezone(datetime.timedelta(hours=8))
    now_bj = datetime.datetime.now(tz_bj)
    now_str = now_bj.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Fetch Important & Latest News via OKX News CLI (Union of Latest + Important)
    news_res_latest = run_json_cmd("okx news latest --lang zh-CN --limit 15 --json") or {}
    news_res_imp = run_json_cmd("okx news important --lang zh-CN --limit 15 --json") or {}
    
    raw_news_latest = news_res_latest.get("details", []) if isinstance(news_res_latest, dict) else []
    raw_news_imp = news_res_imp.get("details", []) if isinstance(news_res_imp, dict) else []
    
    seen_ids = set()
    raw_news = []
    for item in raw_news_latest + raw_news_imp:
        nid = str(item.get("id", ""))
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            raw_news.append(item)
            
    # Sort strictly by creation timestamp descending
    raw_news.sort(key=lambda x: int(x.get("cTime", 0) or 0), reverse=True)
    raw_news = raw_news[:20]

    if not raw_news:
        news_res2 = run_json_cmd("okx news latest --lang zh-CN --limit 15 --json") or {}
        raw_news = news_res2.get("details", []) if isinstance(news_res2, dict) else []

    parsed_news = []
    triggered_threat = None

    for item in raw_news:
        c_time = int(item.get("cTime", 0) or 0) / 1000.0
        dt_str = datetime.datetime.fromtimestamp(c_time, tz=tz_bj).strftime("%Y-%m-%d %H:%M:%S") if c_time > 0 else "--"
        title = item.get("title", "")
        summary = item.get("summary", "")
        full_text = f"{title} {summary}"

        # Only evaluate black-swan patterns for news within last 15 minutes
        if time.time() - c_time < 900:
            for pattern, threat_name in BLACK_SWAN_PATTERNS:
                if re.search(pattern, full_text, re.IGNORECASE):
                    triggered_threat = (title, threat_name)
                    break

        parsed_news.append({
            "id": item.get("id"),
            "time": dt_str,
            "title": title,
            "summary": summary,
            "coins": item.get("ccyList", []),
            "platforms": item.get("platformList", []),
            "importance": item.get("importance", "high"),
            "url": item.get("sourceUrl", "")
        })

    if triggered_threat:
        trigger_circuit_breaker(triggered_threat[0], triggered_threat[1])
    else:
        # If no genuine black-swan is active, ensure circuit breaker is cleared if expired
        if os.path.exists(CIRCUIT_BREAKER_FILE):
            try:
                with open(CIRCUIT_BREAKER_FILE, "r", encoding="utf-8") as f:
                    cb_data = json.load(f)
                if cb_data.get("active") and time.time() >= cb_data.get("expires_at_ts", 0):
                    cb_data["active"] = False
                    with open(CIRCUIT_BREAKER_FILE, "w", encoding="utf-8") as f:
                        json.dump(cb_data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    # 2. Fetch Multi-Coin Sentiment Snapshot
    coins_str = ",".join(TARGET_COINS)
    sent_res = run_json_cmd(f"okx news coin-sentiment --coins {coins_str} --json") or []
    coin_sentiments = {}

    if isinstance(sent_res, list) and sent_res and "details" in sent_res[0]:
        for d in sent_res[0]["details"]:
            ccy = d.get("ccy", "")
            sent = d.get("sentiment", {})
            bull_ratio = float(sent.get("bullishRatio", 0.5) or 0.5)
            bear_ratio = float(sent.get("bearishRatio", 0.1) or 0.1)
            total_mentions = int(d.get("mentionCnt", 0) or 0)
            label = sent.get("label", "neutral")

            net_sentiment = bull_ratio - bear_ratio
            sentiment_score = round(net_sentiment * 0.8, 2)

            coin_sentiments[ccy] = {
                "ccy": ccy,
                "label": label,
                "bullish_ratio": f"{bull_ratio*100:.1f}%",
                "bearish_ratio": f"{bear_ratio*100:.1f}%",
                "mentions": total_mentions,
                "sentiment_factor_score": sentiment_score
            }

    # 3. Overall Macro Sentiment Synthesis
    cb_active, cb_info = is_circuit_breaker_active()
    if cb_active:
        macro_env = "🚨 避险熔断中"
    else:
        bull_count = sum(1 for c, s in coin_sentiments.items() if s["sentiment_factor_score"] > 0.25)
        bear_count = sum(1 for c, s in coin_sentiments.items() if s["sentiment_factor_score"] < -0.1)
        macro_env = "偏多震荡" if bull_count > bear_count else ("偏空承压" if bear_count > bull_count else "中性平衡")

    payload = {
        "timestamp": now_str,
        "updated_at": now_str,
        "macro_sentiment": macro_env,
        "circuit_breaker": cb_info if cb_active else {"active": False},
        "coins_sentiment": coin_sentiments,
        "latest_news": parsed_news[:10]
    }

    # Fail-closed: an upstream hiccup must not wipe a good cache into an empty page.
    if not payload["latest_news"] or not payload["coins_sentiment"]:
        try:
            if os.path.exists(NEWS_CACHE_FILE):
                with open(NEWS_CACHE_FILE, "r", encoding="utf-8") as f:
                    previous = json.load(f)
                if previous.get("latest_news") or previous.get("coins_sentiment"):
                    if not payload["latest_news"] and previous.get("latest_news"):
                        payload["latest_news"] = previous["latest_news"]
                    if not payload["coins_sentiment"] and previous.get("coins_sentiment"):
                        payload["coins_sentiment"] = previous["coins_sentiment"]
                        bull_count = sum(1 for s in previous["coins_sentiment"].values() if float(s.get("sentiment_factor_score", 0)) > 0.25)
                        bear_count = sum(1 for s in previous["coins_sentiment"].values() if float(s.get("sentiment_factor_score", 0)) < -0.1)
                        if not cb_active:
                            payload["macro_sentiment"] = "偏多震荡" if bull_count > bear_count else ("偏空承压" if bear_count > bull_count else "中性平衡")
                    payload["stale_sections"] = True
        except Exception:
            pass

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(NEWS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return payload

if __name__ == "__main__":
    res = fetch_and_analyze_news_sentiment()
    print(f"✅ OKX News & Sentiment Engine complete. Macro: {res['macro_sentiment']}, News Count: {len(res['latest_news'])}")
