"""
main.py —— 「找店助手」桌面软件入口

界面用 HTML 写（放在 app/ui.html），外面套一个 Windows 原生窗口（WebView2）。
这样做的好处：界面能做得跟网页一样细，但用户看到的是一个独立软件窗口，不是浏览器。

前后端的分工：
  界面上点「帮我找」 → 前端调 window.pywebview.api.recommend(一句话)
                     → Python 这边跑 agent.run()，返回结构化结果
                     → 前端负责渲染卡片
"""

import json
import os
import sys
import traceback
from pathlib import Path

import webview

# 打包后强制把工作目录切到 exe 所在目录。
# 原因：从快捷方式、任务栏、别的程序里拉起时，工作目录不一定是 exe 目录，
# WebView2 和 .NET 运行时初始化会因此失败（现象是闪一下就退出，退出码 1）。
if getattr(sys, "frozen", False):
    try:
        os.chdir(Path(sys.executable).parent)
    except Exception:
        pass


# ---------- 路径：源码运行 / 打包后运行，都要能找到 src 和 ui.html ----------
def _root():
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


ROOT = _root()
sys.path.insert(0, str(ROOT / "src"))

import agent                                    # noqa: E402
import baseline                                 # noqa: E402
from evaluate import check_rec, PRICE_RE, DIST_RE   # noqa: E402


# ---------- 前后端接口 ----------
class Api:
    def __init__(self):
        self._slot_cache = {}       # 同一次提问，避免重复解析两遍
        self._out_cache = {}        # 最近一次结果（拖半径滑杆时复用）

    # --- 前端启动时问一次：地图该用哪个 key ---
    def config(self):
        return {
            "amap_js_key": os.getenv("AMAP_JS_KEY", "").strip(),
            "amap_security_code": os.getenv("AMAP_SECURITY_CODE", "").strip(),
            "has_amap_service_key": bool(os.getenv("AMAP_KEY", "").strip()),
            "center": {"name": agent.CENTER["name"],
                       "lat": agent.CENTER["lat"], "lng": agent.CENTER["lng"]},
            "default_radius_km": agent.DEFAULT_RADIUS_KM,
        }

    def _slots_for(self, query):
        if query not in self._slot_cache:
            self._slot_cache[query] = agent.parse_query(query)
        return self._slot_cache[query]

    def _judge(self, query, recs, source="local"):
        """用真实的 POI 数据核对每家店是否真的满足条件，并标出问题。"""
        slots = self._slots_for(query)
        exp = {
            "budget": slots.get("budget"),
            "max_km": slots.get("max_km"),
            "need_room": slots.get("need_room"),
            "category": slots.get("category"),
            "avoid": slots.get("avoid", []),
        }
        by_name = {p["name"]: p for p in agent.POIS}
        out = []
        for r in recs:
            name = r["name"]

            if source == "amap":
                # 高德店不在本地 POI 库里，直接拿推荐时带回来的真实字段核对，
                # 规则跟本地版一致：预算/距离说错照样标出来
                price, dist = r.get("avg_price"), r.get("distance_km")
                notes = []
                if (exp["budget"] is not None and price is not None
                        and price > exp["budget"] and not r.get("price_estimated")):
                    notes.append(f"超预算({price}>{exp['budget']})")
                if exp["max_km"] is not None and dist is not None and dist > exp["max_km"]:
                    notes.append(f"超距离({dist}>{exp['max_km']}km)")
                m = PRICE_RE.search(r.get("reason", ""))
                if m and price is not None:
                    claimed = int(m.group(1) or m.group(2))
                    if abs(claimed - price) > 1:
                        notes.append(f"价格说错({claimed}≠{price})")
                m = DIST_RE.search(r.get("reason", ""))
                if m and dist is not None:
                    claimed = float(m.group(1))
                    if abs(claimed - dist) > 0.5:
                        notes.append(f"距离说错({claimed}≠{dist}km)")
                out.append({
                    "name": name, "reason": r.get("reason", ""),
                    "avg_price": price, "distance_km": dist,
                    "price_estimated": r.get("price_estimated", False),
                    "tags": r.get("tags") or [], "notes": notes,
                    # 可核实性字段：界面靠它们显示"营业中 / 时间未知 / 包间请电话确认"
                    "open_time": r.get("open_time", ""),
                    "open_status": r.get("open_status"),
                    "has_room": r.get("has_room"),
                    "room_hint": r.get("room_hint", False),
                    "tel": r.get("tel", ""),
                    "category_unverified": r.get("category_unverified", False),
                    "photos": r.get("photos", []),
                })
                continue

            poi = by_name.get(name)
            price = r.get("avg_price")
            if price is None and poi:
                price = poi["avg_price"]
            dist = r.get("distance_km")
            if dist is None and poi:
                dist = agent.haversine_km(agent.USER_LOC["lat"], agent.USER_LOC["lng"],
                                          poi["lat"], poi["lng"])

            tags = r.get("tags") or (poi["tags"] if poi else [])
            ok, hallu, notes = check_rec(name, r.get("reason", ""), exp)

            out.append({
                "name": name,
                "reason": r.get("reason", ""),
                "avg_price": price,
                "distance_km": dist,
                "price_estimated": False,
                "tags": tags,
                "notes": notes,
                # 模拟数据是完整标注的：标签里有"有包间"就是有，没有就是没有
                "open_time": poi.get("open_time", "") if poi else "",
                "open_status": None,
                "has_room": ("有包间" in tags) if tags else None,
                "room_hint": False,
                "tel": "",
                "category_unverified": False,
                "photos": [],
            })
        return out

    def _pack(self, query, out):
        source = out.get("source", "local")
        recs = self._judge(query, out["recs"], source)
        return {
            "ok": True,
            "slots": out["slots"],
            "recs": recs,
            "relaxed": out["relaxed"],
            "pool": out["pool"],
            "source": source,
            "map": out.get("map"),
            "notices": out.get("notices", []),
        }

    # --- 工具版：完整四步链路 ---
    def recommend(self, query, radius_km=None):
        try:
            out = agent.run(query, radius_km=radius_km, source="auto")
            self._out_cache[query] = out
            return self._pack(query, out)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "slots": None, "recs": [], "relaxed": False, "pool": 0, "map": None}

    # --- 拖半径滑杆：复用已解析的条件，只重跑检索 ---
    def set_radius(self, query, radius_km):
        try:
            # 前端滑杆给的是字符串，这里统一转成数字；转不了就退回默认值
            try:
                radius_km = float(radius_km) if radius_km is not None else None
            except (TypeError, ValueError):
                radius_km = None
            slots = self._slots_for(query)
            out = agent.run(query, radius_km=radius_km, source="auto", slots=slots)
            self._out_cache[query] = out
            return self._pack(query, out)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "slots": None, "recs": [], "relaxed": False, "pool": 0, "map": None}

    # --- 对照组：纯大模型直答（固定用本地模拟库，演示"不给工具会怎样"）---
    def baseline(self, query):
        try:
            out = baseline.run(query)
            recs = self._judge(query, out["recs"])
            return {"ok": True, "recs": recs}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "recs": []}


def check_env():
    """启动前先确认密钥在，避免用户点半天才发现没配置。只拦真正跑不起来的（大模型）。"""
    if not os.getenv("OPENAI_API_KEY"):
        return ("没有找到 API 密钥。\n\n"
                "请把 .env 文件放在这个程序旁边（和 exe 同一个文件夹），"
                "里面写一行：OPENAI_API_KEY=你的密钥")
    return None


def amap_notes():
    """高德相关的缺失只做提示，不拦着程序跑：没 key 就退回本地模拟数据。"""
    notes = []
    if not os.getenv("AMAP_KEY"):
        notes.append("缺少 AMAP_KEY（高德 Web服务）→ 现在用本地模拟数据")
    if not os.getenv("AMAP_JS_KEY"):
        notes.append("缺少 AMAP_JS_KEY（高德 Web端）→ 右上地图不会加载")
    return notes


def main():
    err = check_env()
    notes = amap_notes()

    window = webview.create_window(
        "找店助手 · 一句话说清你要吃什么",
        str(ROOT / "app" / "ui.html"),
        js_api=Api(),
        width=1180,
        height=780,
        min_size=(960, 640),
        background_color="#f7f8fa",
    )

    def on_loaded():
        # 大模型密钥缺失 → 结果区整块提示（因为真的用不了）
        if err:
            window.evaluate_js(
                "document.querySelector('#result').innerHTML = " +
                json.dumps(f'<div class="err"><b>启动检查未通过</b>{err}</div>', ensure_ascii=False)
            )
        # 高德缺失 → 只在左下那行小字提示，不挡着用
        if notes:
            window.evaluate_js(
                "var h=document.querySelector('#srchint');"
                "if(h){" + f"h.textContent={json.dumps('；'.join(notes), ensure_ascii=False)};"
                "h.style.color='#8a6d0b';}"
            )

    window.events.loaded += on_loaded

    webview.start()


if __name__ == "__main__":
    main()
