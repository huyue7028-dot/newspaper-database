#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四大报刊数据库 - 自动更新脚本
==============================
定期抓取4份电子报刊的最新内容，增量更新到数据库中。

注意：本脚本不生成 data.js 文件。
网站前端直接加载 articles.jsonl，因此 data.js 不再需要。

使用方法：
  python3 update_database.py

CI 模式（GitHub Actions 自动使用，无需手动指定）：
  在 GitHub Actions 环境中，脚本会自动启用 CI 模式：
  - 超时时间缩短为 8 秒（本地 25 秒）
  - 不重试（本地 2 次重试）
  - 爬取范围缩小为最近 2 天（本地 14 天）
  - 全局时间限制 4 分钟，超时后立即停止
  - 即使所有网站无法访问，也正常退出（exit 0）

支持的四份报刊：
  1. 国际出版周报 (yeeipw.cpmj.com.cn)
  2. 中国新闻出版广电报 (epaper.chinaxwcb.com)
  3. 中华读书报 (epaper.gmw.cn/zhdsb)
  4. 文艺报 (wyb.chinawriter.com.cn)
"""

import os
import re
import sys
import json
import time
import random
import urllib3
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urljoin

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================ CI 模式检测 ============================
CI_MODE = os.environ.get("CI", "").lower() in ("true", "1", "yes") or \
          os.environ.get("GITHUB_ACTIONS", "").lower() in ("true", "1", "yes")

if CI_MODE:
    print("[CI 模式] 检测到 GitHub Actions 环境，启用快速更新模式", flush=True)
    TIMEOUT = 8         # CI 模式：8 秒超时（本地 25 秒）
    MAX_RETRY = 0       # CI 模式：不重试（本地 2 次重试）
    CRAWL_DAYS = 2      # CI 模式：只检查最近 2 天（本地 14 天）
    DELAY_MIN, DELAY_MAX = 0.05, 0.15  # CI 模式：极短间隔
    MAX_CRAWL_TIME = 240  # CI 模式：全局爬取时间限制 4 分钟
    MAX_NODES = 3       # CI 模式：最多 3 个版面
else:
    TIMEOUT = 25
    MAX_RETRY = 2
    CRAWL_DAYS = 14
    DELAY_MIN, DELAY_MAX = 0.3, 0.8
    MAX_CRAWL_TIME = 3600  # 本地：1 小时
    MAX_NODES = 50

# ============================ 全局时间控制 ============================
START_TIME = time.time()

def time_remaining():
    """返回剩余可用时间（秒），如果已超时返回 0"""
    elapsed = time.time() - START_TIME
    return max(0, MAX_CRAWL_TIME - elapsed)

def should_stop():
    """检查是否应该停止爬取"""
    return time_remaining() <= 0

# ============================ 配置 ============================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = SCRIPT_DIR
JSONL_PATH = os.path.join(DATA_DIR, "articles.jsonl")
SEEN_URLS_PATH = os.path.join(DATA_DIR, "seen_urls.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ============================ HTTP工具 ============================
def fetch(url, binary=False):
    """带超时的HTTP请求，超时或错误返回None"""
    try:
        r = SESSION.get(url, verify=False, timeout=TIMEOUT)
        if r.status_code == 200:
            result = r.content if binary else r.text
            r.close()
            return result
        r.close()
        if r.status_code in (404, 410):
            return None
    except Exception:
        pass
    # 重试（CI模式MAX_RETRY=0，不重试）
    for attempt in range(MAX_RETRY):
        try:
            r = SESSION.get(url, verify=False, timeout=TIMEOUT)
            if r.status_code == 200:
                result = r.content if binary else r.text
                r.close()
                return result
            r.close()
        except Exception:
            pass
        time.sleep(1)
    return None

def polite_sleep():
    t = random.uniform(DELAY_MIN, DELAY_MAX)
    time.sleep(t)

def decode_response(resp):
    raw = resp.content
    enc = (resp.encoding or "").lower()
    if enc in ("", "iso-8859-1", "latin-1"):
        head = raw[:2048].decode("ascii", errors="ignore")
        m = re.search(r'charset=["\']?\s*([a-z0-9_-]+)', head, re.I)
        enc = m.group(1).lower() if m else "utf-8"
    try:
        return raw.decode(enc or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")

def fetch_auto(url):
    """带解码的HTTP请求，超时或错误返回None"""
    try:
        r = SESSION.get(url, verify=False, timeout=TIMEOUT)
        if r.status_code == 200:
            text = decode_response(r)
            r.close()
            return text
        r.close()
    except Exception:
        pass
    for attempt in range(MAX_RETRY):
        try:
            r = SESSION.get(url, verify=False, timeout=TIMEOUT)
            if r.status_code == 200:
                text = decode_response(r)
                r.close()
                return text
            r.close()
        except Exception:
            pass
        time.sleep(1)
    return None

# ============================ 分类工具 ============================
COUNTRY_MAP = {
    "中国": "中国", "美国": "美国", "英国": "英国", "法国": "法国", "德国": "德国",
    "日本": "日本", "韩国": "韩国", "俄罗斯": "俄罗斯", "意大利": "意大利",
    "西班牙": "西班牙", "荷兰": "荷兰", "印度": "印度", "巴西": "巴西",
    "加拿大": "加拿大", "澳大利亚": "澳大利亚", "博洛尼亚": "意大利",
    "法兰克福": "德国", "伦敦": "英国", "巴黎": "法国", "纽约": "美国",
    "北京": "中国", "上海": "中国",
}

THEME_RULES = [
    ("政策法规", ["政策", "法规", "法律", "条例", "管理办法", "两会", "立法", "监管", "署长", "局长"]),
    ("版权贸易", ["版权", "著作权", "授权", "引进版", "版权输出", "IP开发", "知识产权"]),
    ("数字出版", ["数字出版", "电子书", "有声书", "网络文学", "数字阅读", "融合出版", "融媒体"]),
    ("出版技术", ["印刷", "装帧", "AI出版", "人工智能", "大模型", "按需印刷"]),
    ("阅读推广", ["全民阅读", "世界读书日", "书香", "荐书", "阅读推广", "读书会", "农家书屋"]),
    ("国际交流", ["国际", "海外", "博洛尼亚", "法兰克福", "书展", "汉学家", "一带一路"]),
    ("儿童出版", ["童书", "绘本", "儿童文学", "少儿", "亲子阅读"]),
    ("教育出版", ["教材", "教辅", "课程", "校园阅读", "高考", "中考"]),
    ("学术前沿", ["学术", "学者", "论文", "专著", "古籍整理", "敦煌"]),
    ("文学动态", ["文学", "小说", "诗歌", "散文", "作家", "文学奖", "文坛"]),
    ("书评书介", ["书评", "评介", "新书推荐", "好书榜", "书榜"]),
    ("出版社", ["出版社", "出版集团", "商务印书馆", "中华书局"]),
    ("市场数据", ["市场报告", "销售数据", "码洋", "排行榜", "开卷"]),
    ("行业变革", ["行业变革", "产业转型", "整合", "重组", "高质量发展"]),
    ("文化教育", ["传统文化", "非遗", "博物馆", "故宫", "文化强国"]),
    ("出版动态", ["出版", "新书", "图书", "发布", "推出", "首发"]),
]

def extract_countries(text):
    found, seen = [], set()
    for kw, country in COUNTRY_MAP.items():
        if kw in text and country not in seen:
            seen.add(country)
            found.append(country)
    return ";".join(found)

def classify_themes(title, text, section):
    blob = (title or "") + " " + (text[:600] or "") + " " + (section or "")
    themes = [name for name, kws in THEME_RULES if any(k in blob for k in kws)]
    if not themes:
        themes = ["出版动态"]
    return ";".join(themes)

def is_non_article(title):
    t = (title or "").strip()
    if not t:
        return True
    if re.search(r"(广告|启事|更正|声明|征订|订阅|中缝|报头|报眼|目录|导读)", t):
        return True
    return False

# ============================ 各报刊抓取器 ============================

def crawl_ipw(existing_urls):
    """国际出版周报 - 抓取最新期次"""
    print("\n[国际出版周报] 开始抓取...", flush=True)
    articles = []
    base = "http://yeeipw.cpmj.com.cn"

    now = datetime.now()
    check_days = CRAWL_DAYS if CI_MODE else 90

    for offset in range(0, check_days):
        if should_stop():
            print(f"  时间到，停止抓取（已检查 {offset} 天）", flush=True)
            break

        d = now - timedelta(days=offset)
        ym = d.strftime("%Y-%m")
        day = d.day

        url = f"{base}/html/{ym}/{day:02d}/node_142567.htm"
        html = fetch_auto(url)
        if not html or len(html) < 500:
            continue

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if should_stop():
                break
            if "content_" in a["href"]:
                art_url = urljoin(url, a["href"])
                if art_url in existing_urls:
                    continue

                title = a.get_text(strip=True)
                if is_non_article(title):
                    continue

                date_match = re.search(r'/(\d{4}-\d{2})/(\d{2})/', art_url)
                date = f"{date_match.group(1)}-{date_match.group(2)}" if date_match else ""

                polite_sleep()
                art_html = fetch_auto(art_url)
                full_text = ""
                if art_html:
                    art_soup = BeautifulSoup(art_html, "html.parser")
                    content_div = art_soup.find("div", class_="content") or art_soup.find("div", id="content")
                    if content_div:
                        full_text = content_div.get_text(strip=True)

                articles.append({
                    'title': title, 'url': art_url, 'date': date,
                    'section': '', 'author': '',
                    'summary': full_text[:100] if full_text else '',
                    'countries': extract_countries(title),
                    'themes': classify_themes(title, full_text[:200], ''),
                    'content_length': str(len(full_text)),
                    'status': '已采集',
                    'newspaper': '国际出版周报'
                })
                existing_urls.add(art_url)

        polite_sleep()

    print(f"  新增 {len(articles)} 篇", flush=True)
    return articles


def crawl_chinaxwcb(existing_urls):
    """中国新闻出版广电报 - 抓取最新期次"""
    print("\n[中国新闻出版广电报] 开始抓取...", flush=True)
    articles = []
    base = "https://epaper.chinaxwcb.com"

    now = datetime.now()
    check_days = CRAWL_DAYS

    for days_ago in range(0, check_days):
        if should_stop():
            print(f"  时间到，停止抓取（已检查 {days_ago} 天）", flush=True)
            break

        d = now - timedelta(days=days_ago)
        ym = d.strftime("%Y-%m")
        dd = d.strftime("%d")

        node_url = f"{base}/app_epaper/{ym}/{dd}/node_01.html"
        html = fetch_auto(node_url)
        if not html or len(html) < 500:
            continue

        soup = BeautifulSoup(html, "html.parser")

        node_links = set()
        for a in soup.find_all("a", href=True):
            if "node_" in a["href"] and ".html" in a["href"]:
                node_links.add(urljoin(node_url, a["href"]))
        node_links.add(node_url)

        for nurl in list(node_links)[:MAX_NODES]:
            if should_stop():
                break
            polite_sleep()
            if nurl != node_url:
                html2 = fetch_auto(nurl)
                if not html2:
                    continue
                soup2 = BeautifulSoup(html2, "html.parser")
            else:
                soup2 = soup

            section = ""
            title_tag = soup2.find("title")
            if title_tag:
                sm = re.search(r"第\s*(\d+)\s*版[：:]\s*(.+)", title_tag.get_text())
                if sm:
                    section = f"第{int(sm.group(1)):02d}版:{sm.group(2).strip()}"

            for a in soup2.find_all("a", href=True):
                if should_stop():
                    break
                if "content_" in a["href"]:
                    art_url = urljoin(nurl, a["href"])
                    if art_url in existing_urls:
                        continue

                    title = a.get_text(strip=True)
                    if is_non_article(title):
                        continue

                    polite_sleep()
                    art_html = fetch_auto(art_url)
                    full_text = ""
                    if art_html:
                        art_soup = BeautifulSoup(art_html, "html.parser")
                        content_div = art_soup.find("div", class_="content") or art_soup.find("founder-content")
                        if content_div:
                            full_text = content_div.get_text(strip=True)

                    date = f"{ym}-{dd}"
                    articles.append({
                        'title': title, 'url': art_url, 'date': date,
                        'section': section, 'author': '',
                        'summary': full_text[:100] if full_text else '',
                        'countries': extract_countries(title),
                        'themes': classify_themes(title, full_text[:200], section),
                        'content_length': str(len(full_text)),
                        'status': '已采集',
                        'newspaper': '中国新闻出版广电报'
                    })
                    existing_urls.add(art_url)

    print(f"  新增 {len(articles)} 篇", flush=True)
    return articles


def crawl_zhdsb(existing_urls):
    """中华读书报 - 只抓取最新期次"""
    print("\n[中华读书报] 开始抓取...", flush=True)
    articles = []
    base = "https://epaper.gmw.cn/zhdsb"

    now = datetime.now()
    months_to_check = 1 if CI_MODE else 2

    for offset in range(0, months_to_check):
        if should_stop():
            print(f"  时间到，停止抓取", flush=True)
            break

        d = now - timedelta(days=offset * 30)
        ym = d.strftime("%Y%m")

        xml_url = f"{base}/html/layout/{ym}/period.xml"
        xml = fetch_auto(xml_url)
        if not xml:
            continue

        # 修复：使用 html.parser 替代 xml parser，避免 lxml 依赖
        soup = BeautifulSoup(xml, "html.parser")
        periods = soup.find_all("period")

        if CI_MODE:
            periods = periods[-2:] if len(periods) > 2 else periods

        for p in periods:
            if should_stop():
                break
            try:
                date_tag = p.find("period_date")
                fp_tag = p.find("front_page")
                if not date_tag:
                    continue

                date_str = date_tag.get_text(strip=True)
                front = fp_tag.get_text(strip=True) if fp_tag else "node_01.html"

                if date_str < "2021-01-01":
                    continue

                dd = date_str.split("-")[-1]
                front_url = f"{base}/html/layout/{ym}/{dd}/{front}"

                polite_sleep()
                html = fetch_auto(front_url)
                if not html:
                    continue

                fsoup = BeautifulSoup(html, "html.parser")

                node_links = set()
                for a in fsoup.find_all("a", href=True):
                    if "node_" in a["href"] and ".html" in a["href"]:
                        node_links.add(urljoin(front_url, a["href"]))
                node_links.add(front_url)

                for nurl in list(node_links)[:MAX_NODES]:
                    if should_stop():
                        break
                    polite_sleep()
                    if nurl != front_url:
                        html2 = fetch_auto(nurl)
                        if not html2:
                            continue
                        soup2 = BeautifulSoup(html2, "html.parser")
                    else:
                        soup2 = fsoup

                    section = ""
                    title_tag = soup2.find("title")
                    if title_tag:
                        sm = re.search(r"第\s*(\d+)\s*版[：:]\s*(.+)", title_tag.get_text())
                        if sm:
                            section = f"第{int(sm.group(1)):02d}版:{sm.group(2).strip()}"

                    for a in soup2.find_all("a", href=True):
                        if should_stop():
                            break
                        if "content_" in a["href"]:
                            art_url = urljoin(nurl, a["href"])
                            if art_url in existing_urls:
                                continue

                            title = a.get_text(strip=True)
                            if is_non_article(title):
                                continue

                            polite_sleep()
                            art_html = fetch_auto(art_url)
                            full_text = ""
                            if art_html:
                                art_soup = BeautifulSoup(art_html, "html.parser")
                                content_div = art_soup.find("div", class_="content") or art_soup.find("founder-content")
                                if content_div:
                                    full_text = content_div.get_text(strip=True)

                            articles.append({
                                'title': title, 'url': art_url, 'date': date_str,
                                'section': section, 'author': '',
                                'summary': full_text[:100] if full_text else '',
                                'countries': extract_countries(title),
                                'themes': classify_themes(title, full_text[:200], section),
                                'content_length': str(len(full_text)),
                                'status': '已采集',
                                'newspaper': '中华读书报'
                            })
                            existing_urls.add(art_url)
            except Exception as e:
                print(f"  [跳过期次] {e}", flush=True)
                continue

    print(f"  新增 {len(articles)} 篇", flush=True)
    return articles


def crawl_wyb(existing_urls):
    """文艺报 - 抓取最新期次"""
    print("\n[文艺报] 开始抓取...", flush=True)
    articles = []
    base = "http://wyb.chinawriter.com.cn"

    html = fetch_auto(base + "/")
    if not html:
        print("  无法访问首页", flush=True)
        return articles

    soup = BeautifulSoup(html, "html.parser")

    node_links = set()
    for a in soup.find_all("a", href=True):
        if "node_" in a["href"] and ".html" in a["href"]:
            node_links.add(urljoin(base + "/", a["href"]))

    print(f"  发现 {len(node_links)} 个版面", flush=True)

    for nurl in list(node_links)[:MAX_NODES]:
        if should_stop():
            print(f"  时间到，停止抓取", flush=True)
            break
        polite_sleep()
        html2 = fetch_auto(nurl)
        if not html2:
            continue
        soup2 = BeautifulSoup(html2, "html.parser")

        section = ""
        title_tag = soup2.find("title")
        if title_tag:
            sm = re.search(r"第\s*(\d+)\s*版", title_tag.get_text())
            if sm:
                section = f"第{int(sm.group(1))}版"

        for a in soup2.find_all("a", href=True):
            if should_stop():
                break
            if "content" in a["href"] and ".html" in a["href"]:
                art_url = urljoin(nurl, a["href"])
                if art_url in existing_urls:
                    continue

                title = a.get_text(strip=True)
                if is_non_article(title):
                    continue

                date_match = re.search(r'/(\d{6})/', art_url)
                date = ""
                if date_match:
                    ym = date_match.group(1)
                    date = f"{ym[:4]}-{ym[4:6]}"
                    dm2 = re.search(r'/(\d{6})/(\d{2})/', art_url)
                    if dm2:
                        date = f"{ym[:4]}-{ym[4:6]}-{dm2.group(2)}"

                polite_sleep()
                art_html = fetch_auto(art_url)
                full_text = ""
                if art_html:
                    art_soup = BeautifulSoup(art_html, "html.parser")
                    content_div = art_soup.find("div", class_="content") or art_soup.find("div", class_="article-content")
                    if content_div:
                        full_text = content_div.get_text(strip=True)

                articles.append({
                    'title': title, 'url': art_url, 'date': date,
                    'section': section, 'author': '',
                    'summary': full_text[:100] if full_text else '',
                    'countries': extract_countries(title),
                    'themes': classify_themes(title, full_text[:200], section),
                    'content_length': str(len(full_text)),
                    'status': '已采集',
                    'newspaper': '文艺报'
                })
                existing_urls.add(art_url)

    print(f"  新增 {len(articles)} 篇", flush=True)
    return articles


# ============================ 主流程 ============================
def load_seen_urls():
    if os.path.exists(SEEN_URLS_PATH):
        try:
            with open(SEEN_URLS_PATH, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen_urls(urls):
    with open(SEEN_URLS_PATH, 'w', encoding='utf-8') as f:
        json.dump(list(urls), f, ensure_ascii=False)

def append_to_jsonl(articles):
    with open(JSONL_PATH, 'a', encoding='utf-8') as f:
        for a in articles:
            f.write(json.dumps(a, ensure_ascii=False, separators=(',',':')) + '\n')


def main():
    print("=" * 60, flush=True)
    print(f"四大报刊数据库自动更新 - {datetime.now().strftime('%Y-%m-%d %H:%M')}", flush=True)
    if CI_MODE:
        print(f"[CI 模式] 超时={TIMEOUT}s, 重试={MAX_RETRY}, 天数={CRAWL_DAYS}, 时间限制={MAX_CRAWL_TIME}s", flush=True)
    print("=" * 60, flush=True)

    os.makedirs(DATA_DIR, exist_ok=True)

    # 加载已有URL
    seen_urls = load_seen_urls()
    print(f"已有URL数: {len(seen_urls)}", flush=True)

    # 依次抓取各报刊
    all_new = []
    import gc

    crawlers = [
        ("国际出版周报", crawl_ipw),
        ("中国新闻出版广电报", crawl_chinaxwcb),
        ("中华读书报", crawl_zhdsb),
        ("文艺报", crawl_wyb),
    ]

    for name, crawler in crawlers:
        try:
            new_articles = crawler(seen_urls)
            all_new.extend(new_articles)
            sys.stdout.flush()
        except Exception as e:
            print(f"  [{name}] 错误: {e}", flush=True)
        gc.collect()

        # 检查是否超时
        if should_stop():
            print(f"\n[超时] 全局时间限制已到，跳过剩余报刊", flush=True)
            break

    elapsed = int(time.time() - START_TIME)
    print(f"\n{'='*60}", flush=True)
    print(f"本次新增文章: {len(all_new)} 篇", flush=True)
    print(f"总耗时: {elapsed} 秒", flush=True)

    if all_new:
        append_to_jsonl(all_new)
        save_seen_urls(seen_urls)
        print(f"已写入 articles.jsonl ({len(all_new)} 篇)，seen_urls.json 已更新", flush=True)
    else:
        print("无新增内容", flush=True)
        if CI_MODE:
            print("(GitHub Actions 服务器在海外，部分中国报刊网站可能无法访问，这是正常的)", flush=True)

    print(f"\n更新完成: {datetime.now().strftime('%Y-%m-%d %H:%M')}", flush=True)
    print("=" * 60, flush=True)

    # CI 模式下始终返回 0，避免 workflow 失败
    sys.exit(0)


if __name__ == "__main__":
    main()
