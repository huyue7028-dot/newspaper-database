#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四大报刊数据库 - 自动更新脚本
==============================
定期抓取4份电子报刊的最新内容，增量更新到数据库中。

使用方法：
  python3 update_database.py

支持的四份报刊：
  1. 国际出版周报 (yeeipw.cpmj.com.cn)
  2. 中国新闻出版广电报 (epaper.chinaxwcb.com)
  3. 中华读书报 (epaper.gmw.cn/zhdsb)
  4. 文艺报 (wyb.chinawriter.com.cn)
"""

import os
import re
import json
import time
import random
import urllib3
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urljoin

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================ 配置 ============================
# 自动检测脚本所在目录作为数据目录（无需手动修改路径）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = SCRIPT_DIR
JSONL_PATH = os.path.join(DATA_DIR, "articles.jsonl")
DATA_JS_PATH = os.path.join(DATA_DIR, "data.js")
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

DELAY_MIN, DELAY_MAX = 0.3, 0.8
TIMEOUT = 25
MAX_RETRY = 2

# ============================ HTTP工具 ============================
def fetch(url, binary=False):
    for attempt in range(MAX_RETRY + 1):
        try:
            r = SESSION.get(url, verify=False, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.content if binary else r.text
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            if attempt == MAX_RETRY:
                return None
            time.sleep(1.5)
    return None

def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

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
    for attempt in range(MAX_RETRY + 1):
        try:
            r = SESSION.get(url, verify=False, timeout=TIMEOUT)
            if r.status_code == 200:
                text = decode_response(r)
                r.close()
                return text
            r.close()
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            if attempt == MAX_RETRY:
                return None
            time.sleep(1.5)
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
    print("\n[国际出版周报] 开始抓取...")
    articles = []
    base = "http://yeeipw.cpmj.com.cn"
    
    # 获取当前月份和上个月
    now = datetime.now()
    for offset in range(0, 3):  # 检查最近3个月
        d = now - timedelta(days=offset * 30)
        ym = d.strftime("%Y-%m")
        
        # 尝试每一天的node页面
        for day in range(1, 32):
            url = f"{base}/html/{ym}/{day:02d}/node_142567.htm"
            html = fetch_auto(url)
            if not html or len(html) < 500:
                continue
            
            soup = BeautifulSoup(html, "html.parser")
            # 找文章链接
            for a in soup.find_all("a", href=True):
                if "content_" in a["href"]:
                    art_url = urljoin(url, a["href"])
                    if art_url in existing_urls:
                        continue
                    
                    title = a.get_text(strip=True)
                    if is_non_article(title):
                        continue
                    
                    # 提取日期
                    date_match = re.search(r'/(\d{4}-\d{2})/(\d{2})/', art_url)
                    date = f"{date_match.group(1)}-{date_match.group(2)}" if date_match else ""
                    
                    # 获取正文
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
                        'status': '已采集' if full_text else '已采集',
                        'newspaper': '国际出版周报'
                    })
                    existing_urls.add(art_url)
            
            if len(html) > 500:
                polite_sleep()
    
    print(f"  新增 {len(articles)} 篇")
    return articles


def crawl_chinaxwcb(existing_urls):
    """中国新闻出版广电报 - 抓取最新期次"""
    print("\n[中国新闻出版广电报] 开始抓取...")
    articles = []
    base = "https://epaper.chinaxwcb.com"
    
    now = datetime.now()
    # 只检查最近14天（减少请求量）
    for days_ago in range(0, 14):
        d = now - timedelta(days=days_ago)
        ym = d.strftime("%Y-%m")
        dd = d.strftime("%d")
        
        # 获取版面列表
        node_url = f"{base}/app_epaper/{ym}/{dd}/node_01.html"
        try:
            html = fetch_auto(node_url)
        except Exception:
            continue
        if not html or len(html) < 500:
            continue
        
        soup = BeautifulSoup(html, "html.parser")
        
        # 找到所有版面链接
        node_links = set()
        for a in soup.find_all("a", href=True):
            if "node_" in a["href"] and ".html" in a["href"]:
                node_links.add(urljoin(node_url, a["href"]))
        
        node_links.add(node_url)  # 包含首页
        
        # 从每个版面提取文章
        for nurl in node_links:
            polite_sleep()
            if nurl != node_url:
                html2 = fetch_auto(nurl)
                if not html2:
                    continue
                soup2 = BeautifulSoup(html2, "html.parser")
            else:
                soup2 = soup
            
            # 提取版面名
            section = ""
            title_tag = soup2.find("title")
            if title_tag:
                sm = re.search(r"第\s*(\d+)\s*版[：:]\s*(.+)", title_tag.get_text())
                if sm:
                    section = f"第{int(sm.group(1)):02d}版:{sm.group(2).strip()}"
            
            for a in soup2.find_all("a", href=True):
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
    
    print(f"  新增 {len(articles)} 篇")
    return articles


def crawl_zhdsb(existing_urls):
    """中华读书报 - 只抓取最新2个月的期次"""
    print("\n[中华读书报] 开始抓取...")
    articles = []
    base = "https://epaper.gmw.cn/zhdsb"
    
    now = datetime.now()
    # 只检查最近2个月的period.xml
    for offset in range(0, 2):
        d = now - timedelta(days=offset * 30)
        ym = d.strftime("%Y%m")
        
        xml_url = f"{base}/html/layout/{ym}/period.xml"
        try:
            xml = fetch_auto(xml_url)
        except Exception:
            xml = None
        if not xml:
            continue
        
        try:
            soup = BeautifulSoup(xml, "xml")
        except Exception:
            continue
        periods = soup.find_all("period")
        
        for p in periods:
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
                try:
                    html = fetch_auto(front_url)
                except Exception:
                    continue
                if not html:
                    continue
                
                fsoup = BeautifulSoup(html, "html.parser")
                
                # 找版面链接（限制最多20个版面）
                node_links = set()
                for a in fsoup.find_all("a", href=True):
                    if "node_" in a["href"] and ".html" in a["href"]:
                        node_links.add(urljoin(front_url, a["href"]))
                node_links.add(front_url)
                
                # 只处理前20个版面，避免超时
                for nurl in list(node_links)[:20]:
                    polite_sleep()
                    try:
                        if nurl != front_url:
                            html2 = fetch_auto(nurl)
                            if not html2:
                                continue
                            soup2 = BeautifulSoup(html2, "html.parser")
                        else:
                            soup2 = fsoup
                    except Exception:
                        continue
                    
                    section = ""
                    title_tag = soup2.find("title")
                    if title_tag:
                        sm = re.search(r"第\s*(\d+)\s*版[：:]\s*(.+)", title_tag.get_text())
                        if sm:
                            section = f"第{int(sm.group(1)):02d}版:{sm.group(2).strip()}"
                    
                    for a in soup2.find_all("a", href=True):
                        if "content_" in a["href"]:
                            art_url = urljoin(nurl, a["href"])
                            if art_url in existing_urls:
                                continue
                            
                            title = a.get_text(strip=True)
                            if is_non_article(title):
                                continue
                            
                            polite_sleep()
                            try:
                                art_html = fetch_auto(art_url)
                            except Exception:
                                art_html = None
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
                print(f"  [跳过期次] {e}")
                continue
    
    print(f"  新增 {len(articles)} 篇")
    return articles


def crawl_wyb(existing_urls):
    """文艺报 - 抓取最新期次"""
    print("\n[文艺报] 开始抓取...")
    articles = []
    base = "http://wyb.chinawriter.com.cn"
    
    # 获取首页找最新期
    html = fetch_auto(base + "/")
    if not html:
        print("  无法访问首页")
        return articles
    
    soup = BeautifulSoup(html, "html.parser")
    
    # 找到版面链接
    node_links = set()
    for a in soup.find_all("a", href=True):
        if "node_" in a["href"] and ".html" in a["href"]:
            node_links.add(urljoin(base + "/", a["href"]))
    
    print(f"  发现 {len(node_links)} 个版面")
    
    for nurl in node_links:
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
            if "content" in a["href"] and ".html" in a["href"]:
                art_url = urljoin(nurl, a["href"])
                if art_url in existing_urls:
                    continue
                
                title = a.get_text(strip=True)
                if is_non_article(title):
                    continue
                
                # 提取日期
                date_match = re.search(r'/(\d{6})/', art_url)
                date = ""
                if date_match:
                    ym = date_match.group(1)
                    date = f"{ym[:4]}-{ym[4:6]}"
                    # 从URL中提取日期
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
    
    print(f"  新增 {len(articles)} 篇")
    return articles


# ============================ 主流程 ============================
def load_seen_urls():
    if os.path.exists(SEEN_URLS_PATH):
        with open(SEEN_URLS_PATH, 'r') as f:
            return set(json.load(f))
    return set()

def save_seen_urls(urls):
    with open(SEEN_URLS_PATH, 'w') as f:
        json.dump(list(urls), f)

def append_to_jsonl(articles):
    with open(JSONL_PATH, 'a', encoding='utf-8') as f:
        for a in articles:
            f.write(json.dumps(a, ensure_ascii=False, separators=(',',':')) + '\n')

def regenerate_data_js():
    """从JSONL重新生成data.js"""
    print("\n重新生成data.js...")
    
    articles = []
    seen = set()
    with open(JSONL_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            a = json.loads(line)
            url = a.get('url', '')
            if url in seen:
                continue
            seen.add(url)
            articles.append(a)
    
    with open(DATA_JS_PATH, 'w', encoding='utf-8') as out:
        out.write("const FIELD_NAMES=['title','url','date','section','author','summary','countries','themes','content_length','status','newspaper'];\n")
        out.write("const ARTICLES=[")
        for i, a in enumerate(articles):
            arr = [
                a.get('title',''), a.get('url',''), a.get('date',''),
                a.get('section',''), a.get('author',''), a.get('summary',''),
                a.get('countries',''), a.get('themes',''),
                str(a.get('content_length','0')), a.get('status',''),
                a.get('newspaper','')
            ]
            if i > 0:
                out.write(',')
            out.write(json.dumps(arr, ensure_ascii=False, separators=(',',':')))
        out.write("];\n")
    
    print(f"  data.js 完成: {len(articles)} 篇文章", flush=True)
    
    # 同时生成独立HTML文件（将data.js内嵌到database.html中）
    html_template_path = os.path.join(DATA_DIR, "database.html")
    standalone_path = os.path.join(DATA_DIR, "四大报刊数据库.html")
    
    if os.path.exists(html_template_path):
        print("  生成独立HTML文件...", flush=True)
        with open(html_template_path, 'r', encoding='utf-8') as f:
            html = f.read()
        
        script_tag = '<script src="data.js"></script>'
        pos = html.find(script_tag)
        if pos != -1:
            before = html[:pos]
            after = html[pos + len(script_tag):]
            with open(standalone_path, 'w', encoding='utf-8') as out:
                out.write(before)
                out.write('<script>\n')
                with open(DATA_JS_PATH, 'r', encoding='utf-8') as js:
                    for line in js:
                        out.write(line)
                out.write('</script>\n')
                out.write(after)
            print(f"  独立HTML已生成: {os.path.getsize(standalone_path)/1024/1024:.1f}MB", flush=True)
    
    return len(articles)


def main():
    import sys
    print("=" * 60, flush=True)
    print(f"四大报刊数据库自动更新 - {datetime.now().strftime('%Y-%m-%d %H:%M')}", flush=True)
    print("=" * 60, flush=True)
    
    os.makedirs(DATA_DIR, exist_ok=True)
    
    # 加载已有URL
    seen_urls = load_seen_urls()
    print(f"已有URL数: {len(seen_urls)}", flush=True)
    
    # 依次抓取各报刊
    all_new = []
    import gc
    
    try:
        new_articles = crawl_ipw(seen_urls)
        all_new.extend(new_articles)
        sys.stdout.flush()
    except Exception as e:
        print(f"  [国际出版周报] 错误: {e}", flush=True)
    gc.collect()
    
    try:
        new_articles = crawl_chinaxwcb(seen_urls)
        all_new.extend(new_articles)
        sys.stdout.flush()
    except Exception as e:
        print(f"  [中国新闻出版广电报] 错误: {e}", flush=True)
    gc.collect()
    
    try:
        new_articles = crawl_zhdsb(seen_urls)
        all_new.extend(new_articles)
        sys.stdout.flush()
    except Exception as e:
        print(f"  [中华读书报] 错误: {e}", flush=True)
    gc.collect()
    
    try:
        new_articles = crawl_wyb(seen_urls)
        all_new.extend(new_articles)
        sys.stdout.flush()
    except Exception as e:
        print(f"  [文艺报] 错误: {e}", flush=True)
    gc.collect()
    
    print(f"\n{'='*60}", flush=True)
    print(f"本次新增文章: {len(all_new)} 篇", flush=True)
    
    if all_new:
        # 保存新文章
        append_to_jsonl(all_new)
        save_seen_urls(seen_urls)
        
        # 重新生成data.js
        total = regenerate_data_js()
        print(f"数据库已更新，总计 {total} 篇文章", flush=True)
    else:
        print("无新增内容", flush=True)
    
    print(f"\n更新完成: {datetime.now().strftime('%Y-%m-%d %H:%M')}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
