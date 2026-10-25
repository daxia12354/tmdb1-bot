#!/usr/bin/env python3
"""TMDB 正版查片 Telegram 机器人（最小可用版，仅用 Python 标准库）。

用法（不把任何 Key 写进代码）：
    export TELEGRAM_BOT_TOKEN="从 @BotFather 拿到的 Token"
    export TMDB_API_KEY="你已有的 TMDB v3 API Key"
    # 或者用 TMDB 的 Read Access Token（JWT 形式）：
    # export TMDB_READ_TOKEN="eyJ..."
    python3 bot.py

行为：给机器人发片名 -> 返回 TMDB 资料（中文、评分、上映/首播日期）
+ 中国大陆正版渠道（TMDB watch/providers 数据，来源 JustWatch）。
如果 TMDB 在 CN 区没有列出渠道，会如实说“未列出”，不编造平台名。
命令 /ia <英文片名> -> 在 Internet Archive 公共领域片库搜索，
返回官方 mp4 直链（点开即在线播放，仅限版权过期公版电影）。
新剧热剧没有合法的机器人内播放源：机器人只能告诉你正版平台，
不能在 Telegram 里直接播出版权影片。
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TG_API = "https://api.telegram.org/bot"
TMDB_API = "https://api.themoviedb.org/3"


def http_get_json(url, headers=None, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Tmdb:
    def __init__(self, api_key="", read_token=""):
        self.api_key = api_key
        self.read_token = read_token

    def get(self, path, params=None):
        params = dict(params or {})
        params.setdefault("language", "zh-CN")
        headers = {}
        if self.read_token:
            headers["Authorization"] = "Bearer " + self.read_token
        elif self.api_key:
            params["api_key"] = self.api_key
        return http_get_json(TMDB_API + path, headers=headers, params=params)

    def search(self, query):
        data = self.get("/search/multi", {"query": query, "include_adult": "false"})
        return [r for r in data.get("results", [])
                if r.get("media_type") in ("movie", "tv")][:5]

    def detail_and_providers(self, item):
        kind = item["media_type"]  # movie 或 tv
        detail = self.get(f"/{kind}/{item['id']}")
        try:
            providers = self.get(f"/{kind}/{item['id']}/watch/providers")
        except Exception:
            providers = {}
        return detail, providers


def provider_names(region):
    out = []
    for key, label in (("flatrate", "包月/会员可看"), ("rent", "租赁"),
                       ("buy", "购买")):
        names = [p.get("provider_name") for p in region.get(key, [])]
        if names:
            out.append(f"{label}：{'、'.join(names)}")
    return out


def format_result(detail, providers, kind):
    title = detail.get("title") or detail.get("name") or "未知标题"
    date = detail.get("release_date") or detail.get("first_air_date") or "日期未知"
    rating = detail.get("vote_average")
    rating_text = f"{rating:.1f}" if isinstance(rating, (int, float)) else "暂无评分"
    overview = (detail.get("overview") or "暂无简介").strip()
    if len(overview) > 300:
        overview = overview[:300] + "…"
    lines = [
        f"<b>{html.escape(title)}</b>",
        f"类型：{'电影' if kind == 'movie' else '剧集'} · 日期：{html.escape(str(date))} · 评分：{rating_text}",
        html.escape(overview),
    ]
    region = (providers.get("results") or {}).get("CN") or {}
    prov_lines = provider_names(region)
    if prov_lines:
        lines.append("正版渠道（中国大陆，TMDB/JustWatch 数据）：")
        lines.extend("· " + html.escape(p) for p in prov_lines)
        if region.get("link"):
            lines.append(f"查看详情：{html.escape(region['link'])}")
    else:
        lines.append("正版渠道：中国大陆暂未在 TMDB 列出（覆盖可能不全，不代表一定没有）。")
    return "\n".join(lines)


def format_others(results, top_id):
    others = [r for r in results
              if r.get("id") != top_id and (r.get("title") or r.get("name"))]
    if not others:
        return ""
    names = "、".join(html.escape(r.get("title") or r.get("name")) for r in others[:4])
    return "\n\n其他可能：" + names


IA_API = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata"


def ia_search(keyword):
    """在 IA 公共领域电影库按英文片名搜索，返回 [{identifier, title}]。"""
    q = f'title:({keyword}) AND mediatype:movies'
    data = http_get_json(IA_API, params={
        "q": q, "fl[]": "identifier,title", "rows": "5",
        "page": "1", "output": "json"})
    docs = (data.get("response") or {}).get("docs") or []
    return [d for d in docs if d.get("identifier")]


def ia_play_url(identifier):
    """取 IA 条目里最适合在线播放的 mp4（优先 512Kb 版本，体积小起播快）。"""
    meta = http_get_json(f"{IA_META}/{urllib.parse.quote(identifier)}")
    files = [f for f in (meta.get("files") or [])
             if (f.get("name") or "").lower().endswith(".mp4")]
    if not files:
        return None
    def rank(f):
        name = (f.get("name") or "").lower()
        return (0 if "512kb" in name else 1,
                int(f.get("size") or 0))
    files.sort(key=rank)
    name = files[0]["name"]
    return ("https://archive.org/download/"
            + urllib.parse.quote(identifier) + "/"
            + urllib.parse.quote(name))


def format_ia(keyword):
    try:
        docs = ia_search(keyword)
    except Exception as exc:  # noqa: BLE001
        return f"公版库搜索出错：{exc}，稍后再试。"
    if not docs:
        return (f"公版库里没搜到“{keyword}”。\n"
                "注意：公版库只有版权过期的老电影（英文片名搜，如 Chaplin），"
                "新剧热剧不在这里；新片请直接发片名查 TMDB，看它列出的正版平台。")
    lines = [f"公版电影搜索“{keyword}”结果（点链接在线播放）："]
    for d in docs[:5]:
        ident = d["identifier"]
        try:
            url = ia_play_url(ident)
        except Exception:  # noqa: BLE001
            url = None
        title = d.get("title") or ident
        if url:
            lines.append(f"· {html.escape(str(title))}\n  {html.escape(url)}")
        else:
            lines.append(f"· {html.escape(str(title))}（暂无 mp4 文件）")
    return "\n".join(lines)


def tg_send(token, chat_id, text):
    return http_post_json(
        f"{TG_API}{token}/sendMessage",
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True})


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tmdb = Tmdb(api_key=os.environ.get("TMDB_API_KEY", "").strip(),
                read_token=os.environ.get("TMDB_READ_TOKEN", "").strip())
    if not token:
        print("缺少 TELEGRAM_BOT_TOKEN（去 @BotFather 申请）", file=sys.stderr)
        return 2
    if not tmdb.api_key and not tmdb.read_token:
        print("缺少 TMDB_API_KEY 或 TMDB_READ_TOKEN", file=sys.stderr)
        return 2
    offset = None
    print("机器人已启动，等待片名…（Ctrl+C 停止）")
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            updates = http_get_json(
                f"{TG_API}{token}/getUpdates", params=params, timeout=40)
            for upd in updates.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                if not text or chat_id is None:
                    continue
                if text.startswith("/start") or text.startswith("/help"):
                    reply = ("发我一部电影或剧集的名字，我回你 TMDB 资料 + "
                             "中国大陆正版渠道。\n例如：奥本海默\n"
                             "想直接在手机上播公版老电影：/ia Chaplin\n"
                             "（仅限版权过期公版电影，新剧热剧没有合法在线源，"
                             "机器人只能告诉你正版平台）")
                elif text.startswith("/ia"):
                    keyword = text[3:].strip()
                    if not keyword:
                        reply = "用法：/ia Chaplin（英文片名）"
                    else:
                        reply = format_ia(keyword)
                else:
                    try:
                        results = tmdb.search(text)
                        if not results:
                            reply = f"没搜到“{text}”，换个名字或英文原名试试。"
                        else:
                            top = results[0]
                            detail, providers = tmdb.detail_and_providers(top)
                            reply = format_result(detail, providers, top["media_type"])
                            reply += format_others(results, top["id"])
                    except Exception as exc:  # noqa: BLE001 - 回给用户可读提示
                        reply = f"查片时出错了：{exc}，稍后再试一次。"
                tg_send(token, chat_id, reply)
        except KeyboardInterrupt:
            print("\n已停止。")
            return 0
        except Exception as exc:  # noqa: BLE001 - 轮询要能自愈
            print(f"轮询出错，5 秒后重试：{exc}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
