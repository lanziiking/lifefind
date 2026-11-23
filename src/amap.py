"""
amap.py —— 高德开放平台接入层（Web 服务 API）

为什么单独一个文件：
  高德返回的数据结构跟项目内部的 POI 结构不一样（它没有"人均价格"、没有"包间"标签，
  菜系藏在 type 字段的一串分级里）。所有"翻译"都放在这一层，
  上层 agent.py 拿到的永远是项目自己的那套字段，换数据源时不用改业务逻辑。

用到的接口：周边搜索 /v3/place/around
  参数 radius = 搜索半径（米），返回的每家店带 distance 字段（米）
  → 距离约束由高德先兜一层，我们再核一遍，这就是"硬约束用代码算"的落地方式。
"""

import json
import os
import re
import urllib.parse
import urllib.request

AMAP_BASE = "https://restapi.amap.com/v3"
TIMEOUT = 12

# 高德"餐饮服务"大类
TYPES_FOOD = "050000"


class AmapError(RuntimeError):
    """高德返回 status != 1 时抛这个，上层好统一处理（比如降级回本地数据）。"""


def api_key():
    return os.getenv("AMAP_KEY", "").strip()


def has_key():
    return bool(api_key())


def _get(path, params):
    params = dict(params)
    params["key"] = api_key()
    url = f"{AMAP_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "lifefind-agent/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    if str(data.get("status")) != "1":
        raise AmapError(f"{data.get('info')}（infocode={data.get('infocode')}）")
    return data


# ---------- 菜系映射 ----------
# 高德的 type 长这样："餐饮服务;中餐厅;湖南菜(湘菜)"
# 我们从后往前找关键词，把它归到项目自己的菜系枚举里
CATEGORY_HINTS = [
    ("潮汕牛肉火锅", ["潮汕牛肉"]),
    ("粤菜", ["粤菜", "广东菜", "广府菜", "顺德菜"]),
    ("潮汕菜", ["潮州菜", "潮汕菜", "潮汕"]),
    ("川菜", ["四川菜", "川菜", "重庆菜"]),
    ("湘菜", ["湖南菜", "湘菜"]),
    ("火锅", ["火锅", "涮肉", "打边炉"]),
    ("日式烤肉", ["日式烤肉", "炭火烤肉", "和牛"]),
    ("日料", ["日本料理", "日式", "寿司", "居酒屋", "拉面"]),
    ("烧烤", ["烧烤", "烤串", "串店", "烤吧"]),
    ("西餐", ["西餐", "牛排", "意大利菜", "法国菜", "披萨"]),
    ("东南亚菜", ["东南亚", "泰国菜", "越南菜", "印度菜", "新马"]),
    ("面食", ["面馆", "面食", "拉面馆", "小吃面"]),
    ("海鲜", ["海鲜", "水产"]),
    ("素食", ["素食", "素菜", "斋菜"]),
    ("西北菜", ["西北菜", "新疆菜", "陕西菜", "兰州"]),
    ("江浙菜", ["江浙菜", "杭帮菜", "上海菜", "淮扬菜", "苏浙"]),
    ("甜品咖啡", ["咖啡", "甜品", "蛋糕", "茶饮", "奶茶"]),
    ("快餐简餐", ["快餐", "简餐", "小吃", "便当", "汉堡"]),
]


def guess_category(name, type_str):
    """
    把高德的一串分类，归到项目的菜系枚举里。

    注意这里**绝对不能**因为"用户想吃粤菜"就把所有店都标成粤菜 ——
    那样菜系约束就变成自我满足，等于没有过滤（曾经踩过这个坑）。
    只有 type 和店名里真的能看出菜系，才认；看不出来的一律标"其他"，
    由上层决定怎么对待（现在是：不排除，但不加分）。
    """
    text = f"{type_str or ''};{name or ''}"
    for cat, hints in CATEGORY_HINTS:
        for h in hints:
            if h in text:
                return cat
    return "其他"


# 用户说的菜系，和别的叫法/近亲。用于"店名里能认出来"的补充判断。
# 只放高置信度的词，避免误判。
CATEGORY_ALIASES = {
    "粤菜": ["粤菜", "广东菜", "广府", "顺德", "粤式", "广式", "茶餐厅", "烧腊",
             "烧味", "煲仔", "老火汤", "港式", "客家菜", "点心", "早茶"],
    "潮汕菜": ["潮汕", "潮州", "汕头", "粿条", "牛肉丸", "砂锅粥", "打冷", "卤鹅"],
    "潮汕牛肉火锅": ["潮汕牛肉", "牛肉火锅"],
    "川菜": ["川菜", "四川", "重庆", "水煮", "回锅", "麻辣香锅", "钵钵鸡"],
    "湘菜": ["湘菜", "湖南", "长沙", "剁椒", "辣椒炒肉"],
    "火锅": ["火锅", "涮肉", "打边炉", "串串香"],
    "日料": ["日料", "日本料理", "寿司", "刺身", "居酒屋", "天妇罗", "烧鸟", "鳗鱼"],
    "日式烤肉": ["日式烤肉", "和牛", "炭火烤肉", "烧肉"],
    "西餐": ["西餐", "牛排", "意式", "意大利", "法式", "披萨", "pizza", "牛扒"],
    "东南亚菜": ["东南亚", "泰国", "越南", "泰式", "越式", "印度", "新马", "咖喱"],
    "烧烤": ["烧烤", "烤串", "串吧", "烤吧", "BBQ"],
    "面食": ["面馆", "刀削", "牛肉面", "拌面", "拉面馆", "面食"],
    "海鲜": ["海鲜", "水产", "渔村", "渔港"],
    "素食": ["素食", "素菜", "斋菜"],
    "西北菜": ["西北", "新疆", "陕西", "兰州", "大盘鸡", "拉条子"],
    "江浙菜": ["江浙", "杭帮", "上海菜", "淮扬", "苏浙", "小笼", "生煎"],
    "甜品咖啡": ["咖啡", "甜品", "蛋糕", "奶茶", "茶饮"],
    "快餐简餐": ["快餐", "简餐", "汉堡", "炸鸡", "麦当劳", "肯德基", "必胜客", "便当"],
}

# 同一个大族里的菜系，用户说 A 时 B 也算沾边（否则"粤菜"会漏掉潮汕菜和潮汕牛肉火锅）
CATEGORY_FAMILY = {
    "粤菜": "粤菜族", "潮汕菜": "粤菜族", "潮汕牛肉火锅": "粤菜族",
}


def category_matches(target, category, name="", type_str=""):
    """
    判断一家店能不能算"用户想吃的那类菜"。

    规则按严格程度递减：
      1. 归类结果和目标完全一致 → 通过
      2. 同一菜系族（粤菜 / 潮汕菜 / 潮汕牛肉火锅）→ 通过
      3. 归类不出来（高德 type 只写"中餐厅"这类）→ 放行，不误杀
      4. 店名/标签里能看出目标菜系的别名 → 通过
      5. 其余（明确是别的菜系）→ 淘汰
    """
    if not target:
        return True
    if category == target:
        return True
    if CATEGORY_FAMILY.get(category) and CATEGORY_FAMILY.get(category) == CATEGORY_FAMILY.get(target):
        return True
    if category == "其他":
        return True
    text = f"{name or ''}{type_str or ''}"
    return any(a in text for a in CATEGORY_ALIASES.get(target, []))


# 按菜系收窄高德的 types，能让返回结果干净很多（不然会混进麦当劳、奶茶店）
TYPES_BY_CATEGORY = {
    "粤菜": "050100", "潮汕菜": "050100", "潮汕牛肉火锅": "050100",
    "川菜": "050100", "湘菜": "050100", "江浙菜": "050100",
    "西北菜": "050100", "海鲜": "050100", "素食": "050100",
    "火锅": "050100", "烧烤": "050100", "面食": "050100",
    "日料": "050200", "日式烤肉": "050200", "西餐": "050200", "东南亚菜": "050200",
    "快餐简餐": "050300",
    "甜品咖啡": "050500|050700|050800|050900",
}


# 估算人均：高德大部分店没有 cost 字段，按菜系给一个区间中位值。
# 这不是"编数据"，是明确标注的估算——界面上会写"约"。
PRICE_ESTIMATE = {
    "粤菜": 110, "潮汕菜": 90, "川菜": 80, "湘菜": 75, "火锅": 95,
    "潮汕牛肉火锅": 105, "日料": 160, "日式烤肉": 150, "烧烤": 85,
    "西餐": 140, "东南亚菜": 100, "面食": 35, "海鲜": 180, "素食": 70,
    "西北菜": 65, "江浙菜": 120, "快餐简餐": 30, "甜品咖啡": 45,
}


def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


# ---------- 营业时间：把一串文字变成"能不能在某个时刻去" ----------
# 高德给的原文长这样（实测）：
#   open_time   "11:00-14:00 17:00-21:00"
#   opentime2   "周一至周日 11:00-14:00,17:00-21:00"
# 也有 [ ] 空数组、也有 "09:30-15:00 18:00-22:30"。
# 跨夜的情况（"17:00-02:00"）必须单独处理，否则会被判成关门。
_OPEN_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})")


def parse_open_time(biz):
    """
    返回 (原文, 区间列表)，区间用"从 0 点起的分钟数"表示，跨夜的那段 end 会超过 1440。

    为什么不用简单的字符串匹配：用户说"晚上10点"，要判断的是
    "22:00 是否落在 11:00-14:00 或 17:00-21:00 之间"，
    这是个区间判断，不是关键词判断，必须解析成数字才算得对。
    """
    raw = biz.get("opentime2") or biz.get("open_time") or ""
    if isinstance(raw, list):
        raw = " ".join(str(x) for x in raw)
    raw = str(raw).strip()
    if not raw:
        return "", []

    ranges = []
    for h1, m1, h2, m2 in _OPEN_RE.findall(raw):
        s = int(h1) * 60 + int(m1)
        e = int(h2) * 60 + int(m2)
        if e <= s:            # 17:00-02:00 这种跨夜店，结束时间算到第二天
            e += 1440
        ranges.append((s, e))
    return raw, ranges


def is_open_at(ranges, minute):
    """
    在某个时刻（从 0 点起的分钟数）是否营业。
    返回 True / False / None —— None 表示"数据里没有营业时间，无法核实"。

    这个三态（是 / 否 / 不知道）是整个项目的核心产品原则：
      能核的就核，核不了的就直说核不了，绝不假装核过。
    """
    if not ranges:
        return None
    for s, e in ranges:
        if s <= minute <= e:
            return True
        if minute + 1440 <= e and s <= minute + 1440:   # 次日凌晨的跨夜段
            return True
    return False


def hhmm_to_minute(text):
    """'22:00' / '22点' / '晚10点' 之类先由模型归一成 HH:MM，这里只做最后的换算。"""
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s*[:：点时]\s*(\d{1,2})?", str(text))
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def _norm(poi):
    """高德 POI → 项目内部 POI。字段对不上的地方这里补齐并打标记。"""
    loc = (poi.get("location") or "").split(",")
    if len(loc) != 2:
        return None
    try:
        lng, lat = float(loc[0]), float(loc[1])
    except ValueError:
        return None

    biz = poi.get("biz_ext") or {}
    if not isinstance(biz, dict):
        biz = {}

    type_str = poi.get("type") or ""
    name = poi.get("name") or ""
    category = guess_category(name, type_str)

    cost = _num(biz.get("cost"))
    rating = _num(biz.get("rating"))
    distance_m = _num(poi.get("distance"))

    tags = []
    seg = [s for s in type_str.split(";") if s]
    if len(seg) >= 3:
        tags.append(seg[-1])
    # 高德没有"有没有包间"这个字段，只能从店名/分类找弱信号，
    # 所以只敢写成"可能有包间"，绝不当成确定事实。
    # has_room 保持 None（= 数据源不提供），上层据此在卡片上写"请电话确认"。
    room_hint = any(k in f"{name}{type_str}" for k in ("包厢", "包间", "宴会", "私房", "酒楼"))
    if room_hint:
        tags.append("可能有包间")
    if poi.get("tel"):
        tags.append("有电话")

    open_raw, open_ranges = parse_open_time(biz)

    # 高德每家店最多带 10 张实拍图，取前 3 张就够卡片展示。
    # 实测原图约 500KB/张，高德没有可用的缩略图参数（type=thumb 只返回 568B 占位图），
    # 所以前端一律懒加载，别一次性把 9 张图全拉下来。
    photos = []
    for ph in (poi.get("photos") or []):
        if isinstance(ph, dict):
            u = (ph.get("url") or "").strip()
            # 高德部分图片给的是 http，桌面窗口里会触发混合内容拦截 → 统一升级成 https
            if u.startswith("http://"):
                u = "https://" + u[len("http://"):]
            if u and u not in photos:
                photos.append(u)
        if len(photos) >= 3:
            break

    return {
        "id": poi.get("id") or name,
        "name": name,
        "category": category,
        "avg_price": int(cost) if cost else PRICE_ESTIMATE.get(category, 80),
        "price_estimated": not bool(cost),
        "rating": rating if rating else 4.0,
        "rating_estimated": not bool(rating),
        "lat": lat,
        "lng": lng,
        "distance_km": round(distance_m / 1000, 2) if distance_m else None,
        "tags": tags,
        "signature": seg[-1] if seg else "",
        "address": poi.get("address") or "",
        "tel": poi.get("tel") or "",
        "type_str": type_str,
        "source": "amap",
        # 新增：营业时间与包间的"能不能核"
        "open_time": open_raw,
        "open_ranges": open_ranges,
        "has_room": None,          # 高德不提供 → 未知，不是"没有"
        "room_hint": room_hint,    # 只是弱信号，不能当结论
        "photos": photos,          # 实拍图，可能为空
    }


def around(lng, lat, radius_m, keywords=None, pages=2, page_size=25, types=None):
    """周边搜索。pagination 是为了拿到足够多的候选（约束一叠加就所剩无几）。"""
    radius_m = float(radius_m or 3000)      # 滑杆/配置可能传字符串，统一成数字
    out, seen = [], set()
    for page in range(1, pages + 1):
        params = {
            "location": f"{lng:.6f},{lat:.6f}",
            "radius": int(min(max(radius_m, 100), 50000)),
            "types": types or TYPES_FOOD,
            "offset": page_size,
            "page": page,
            "extensions": "all",
            "sortrule": "distance",
        }
        if keywords:
            params["keywords"] = keywords
        data = _get("/place/around", params)
        pois = data.get("pois") or []
        if not pois:
            break
        for p in pois:
            item = _norm(p)
            if not item or item["id"] in seen:
                continue
            seen.add(item["id"])
            out.append(item)
        if len(pois) < page_size:
            break
    return out


def search(category, center, radius_km, pages=2):
    """
    按用户的菜系搜索。category 为空时搜"餐饮服务"大类。

    这里只用高德做两件事：**范围内有哪些店、各自在哪**。
    "是不是用户要的菜系"不交给高德的关键词搜索 ——
    实测 /place/around 传 keywords="粤菜" 会返回周边所有餐厅（麦当劳都在里面），
    所以菜系判断放到本地 category_matches 里做，可控、可测、讲得清楚。
    """
    types = TYPES_BY_CATEGORY.get(category, TYPES_FOOD) if category else TYPES_FOOD
    return around(center["lng"], center["lat"], radius_km * 1000,
                  pages=pages, types=types)
