"""
agent.py —— 自然语言找店助手 v2（有工具、有硬约束校验的版本）

流程四步（跟美团"小团"的思路一致）：
  1. 需求解析：让大模型把一句大白话拆成结构化条件（人数/预算/距离/包间/菜系/忌口）
  2. 检索：从 POI 库里按菜系、关键词召回候选
  3. 硬约束过滤 + 排序：预算、距离、包间这三件事【用代码算】，绝不交给模型估
  4. 生成方案：把筛选后的真实字段交给模型写推荐理由，禁止它编造

这份代码的重点不是"炫技"，而是体现一个产品判断：
  凡是有明确对错的约束（钱、距离、有没有包间），必须用规则算；
  凡是主观的（哪家更好吃、氛围合不合适），才交给模型。
"""

import json
import math
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

import amap                      # 高德接入层：真实商户数据


# ---------- 路径处理：源码运行 和 打包成 exe 后运行，都要能找到文件 ----------
def _base_dir():
    if getattr(sys, "frozen", False):                    # 打包后（PyInstaller）
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent        # 源码运行


BASE_DIR = _base_dir()

# .env 的查找顺序：exe 同目录优先（方便换密钥），再读打包内置的兜底
if getattr(sys, "frozen", False):
    load_dotenv(Path(sys.executable).parent / ".env")
load_dotenv(BASE_DIR / ".env")

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    timeout=45,        # 不加这行，某个请求挂住会卡 10 分钟（SDK 默认 600 秒）
    max_retries=2,
)
MODEL = "qwen-plus"             # 槽位抽取和生成都用它：快、稳、够用
                                # （换成 qwen3.7-plus 质量更好，但并发跑评测会被限流拖慢 10 倍）
DATA = json.loads((BASE_DIR / "data" / "pois.json").read_text(encoding="utf-8"))
POIS = DATA["pois"]
USER_LOC = DATA["_meta"]["user_location"]

# 菜系枚举：限制模型的选择范围，避免它自由发挥出"创意菜"这种库里没有的词
CATEGORIES = [
    "粤菜", "潮汕菜", "川菜", "湘菜", "火锅", "潮汕牛肉火锅", "日料",
    "日式烤肉", "烧烤", "西餐", "东南亚菜", "面食", "海鲜", "素食",
    "西北菜", "江浙菜", "快餐简餐", "甜品咖啡",
]


# ---------- 工具1：算距离（球面距离，单位公里） ----------
def haversine_km(lat1, lng1, lat2, lng2):
    """两点间的球面距离。这就是"距离必须用代码算"的那部分。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 2)


def with_distance(poi):
    """给每家店补上"离你多远"这个字段，后面排序和展示都要用。"""
    item = dict(poi)
    item["distance_km"] = haversine_km(USER_LOC["lat"], USER_LOC["lng"], poi["lat"], poi["lng"])
    return item


# ---------- 第一步：需求解析（槽位抽取） ----------
PARSE_PROMPT = """你是本地生活产品的需求解析模块。把用户的一句话拆成结构化条件。

字段说明：
- people：用餐人数，整数。用户没说就填 null。
- budget：人均预算上限（元），整数。"人均100左右""100以内"都填 100。没说填 null。
- max_km：可接受的最远距离（公里），数字。"3公里内"填 3，"附近""别太远"这种不明确的填 null。
- need_room：是否需要包间。明确提到包间/包厢填 true，没提填 null。
- category：菜系，只能从下面列表里选一个，没说或说不清填 null。
- avoid：忌口或要排除的东西，数组，比如 ["辣"]、["香菜"]。没有填 []。
- at_time：计划几点用餐，24 小时制 "HH:MM"。只有用户明确说了时间才填：
  "晚上10点"→"22:00"，"中午"→"12:00"，"凌晨1点"→"01:00"。
  没说、或只说"晚上""改天"这种含糊的，填 null。

菜系列表：{categories}

只输出 JSON，不要任何解释。格式：
{{"people": null, "budget": null, "max_km": null, "need_room": null, "category": null, "avoid": [], "at_time": null}}

用户说：{query}"""


def parse_query(query):
    """把自然语言变成 6 个字段。抽错了后面全错，所以这是产品的第一道关口。"""
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": PARSE_PROMPT.format(categories="、".join(CATEGORIES), query=query),
        }],
        temperature=0,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content.strip()
    slots = json.loads(text)
    # 补齐缺失字段，避免后面 KeyError
    for k, v in {"people": None, "budget": None, "max_km": None,
                 "need_room": None, "category": None, "avoid": [],
                 "at_time": None}.items():
        slots.setdefault(k, v)
    if not isinstance(slots.get("avoid"), list):
        slots["avoid"] = []
    return slots


# ---------- 第二、三步：召回 + 硬约束过滤 + 排序 ----------
def retrieve_and_rank(slots, top_k=3):
    """
    硬约束（钱、距离、包间）在这里用 if 判断掉，一个都不许漏。
    软约束（评分、距离远近、安静/带娃等标签）用来排序，决定谁排前面。
    """
    cands = [with_distance(p) for p in POIS]

    # ① 菜系召回：用户点了菜系，就只在同类里找
    if slots.get("category"):
        same = [c for c in cands if c["category"] == slots["category"]]
        if same:
            cands = same

    # ② 硬约束过滤：预算、距离、包间
    budget = slots.get("budget")
    max_km = slots.get("max_km")
    need_room = slots.get("need_room")

    filtered = []
    for c in cands:
        if budget is not None and c["avg_price"] > budget:
            continue                                  # 超预算，直接淘汰
        if max_km is not None and c["distance_km"] > max_km:
            continue                                  # 超距离，直接淘汰
        if need_room is True and "有包间" not in c["tags"]:
            continue                                  # 要包间但没包间，淘汰
        filtered.append(c)

    # 如果全被筛没了，就放宽成"只在菜系里挑最近的"，同时把情况告诉用户
    relaxed = False
    if not filtered:
        relaxed = True
        filtered = [with_distance(p) for p in POIS]
        if slots.get("category"):
            same = [c for c in filtered if c["category"] == slots["category"]]
            if same:
                filtered = same

    # ③ 软约束打分：评分高的、离得近的、标签对得上的往上排
    def score(c):
        s = (c["rating"] - 4.0) * 3          # 评分是主要参考
        s -= c["distance_km"] * 0.25          # 近的加分
        if need_room is True and "有包间" in c["tags"]:
            s += 1.0
        return s

    filtered.sort(key=score, reverse=True)
    return filtered[:top_k], len(filtered), relaxed


# ---------- 数据源二：高德真实商户 ----------
# 中心点暂时固定成"珠江新城地铁站"（跟本地数据保持同一套坐标），
# 想换成"我的当前位置"，只要改这里或让前端传进来。
CENTER = {"name": USER_LOC["name"], "lat": USER_LOC["lat"], "lng": USER_LOC["lng"]}
DEFAULT_RADIUS_KM = 3.0


def _amap_filter_rank(cands, slots, top_k=3):
    """
    高德拿回来的候选，一样要走硬约束，但这里有个必须诚实处理的点：
      · 距离：高德按半径搜，天然就在圈内，我们再核一遍 distance_km
      · 预算：高德大部分人没有真实人均，用的是估算价 → 估算的店不打淘汰，
              只标记出来（宁可多给一家，也不因为估算误差误杀好店）
      · 包间：高德压根没这个字段 → 需要包间时不过滤，只把"可能有包间"的往前排
    这三条就是"数据源换了，产品规则要跟着调整"的具体体现，面试可以直接讲。
    """
    budget = slots.get("budget")
    max_km = slots.get("max_km")
    need_room = slots.get("need_room")
    category = slots.get("category")
    at_min = amap.hhmm_to_minute(slots.get("at_time"))

    kept, unsure = [], []
    for c in cands:
        # 菜系：高德的关键词搜索不按菜系过滤（实测传"粤菜"会返回周边所有餐厅），
        # 所以这里必须自己判一遍，否则会出现"要粤菜却推湘菜"
        c["category_unverified"] = False
        if category:
            if not amap.category_matches(
                    category, c.get("category"), c.get("name"), c.get("type_str")):
                continue
            # 高德的 type 大量只写"中餐厅"，从分类根本看不出菜系（实测搜湘菜时，
            # 25 家里只有 2 家真标了"湖南菜(湘菜)"）。店名里也没有菜系线索的，
            # 只能算"可能是"，不能算"是" —— 这类单独放一边，只用来补位。
            alias_hit = any(a in f"{c.get('name', '')}{c.get('type_str', '')}"
                            for a in amap.CATEGORY_ALIASES.get(category, []))
            if c.get("category") == "其他" and not alias_hit:
                c["category_unverified"] = True
        if max_km is not None and c.get("distance_km") is not None and c["distance_km"] > max_km:
            continue
        # 只有"真实价格"超过预算才淘汰；估算价只在排序里降权
        if budget is not None and not c.get("price_estimated") and c["avg_price"] > budget:
            continue
        # 营业时间：这是新加的硬约束，也是最典型的三态判断
        #   能查到且已关门 → 淘汰（用户晚上10点去，21点关门的店推给他就是害他）
        #   能查到且还开着 → 通过
        #   查不到营业时间   → 保留，但标记"未知"，界面上要写出来
        if at_min is not None:
            st = amap.is_open_at(c.get("open_ranges") or [], at_min)
            if st is False:
                continue
            c["open_status"] = "open" if st else "unknown"
        else:
            c["open_status"] = None
        (unsure if c["category_unverified"] else kept).append(c)

    def score(c):
        s = ((c.get("rating") or 4.0) - 4.0) * 3
        s -= (c.get("distance_km") or 0) * 0.25
        if category and c.get("category") == category:
            s += 1.5                       # 菜系判断明确的优先于"看不出菜系"的
        if need_room is True and "可能有包间" in c.get("tags", []):
            s += 1.0
        if budget is not None and c.get("price_estimated") and c["avg_price"] > budget:
            s -= 0.8                       # 估算价超预算 → 往后排，但不淘汰
        # 营业时间确定的优先于"查不到"的：同样符合条件，能核实的更靠得住
        if c.get("open_status") == "open":
            s += 0.4
        elif c.get("open_status") == "unknown":
            s -= 0.3
        if c.get("category_unverified"):
            s -= 1.2                      # 菜系没确认的，往后排
        return s

    kept.sort(key=score, reverse=True)
    unsure.sort(key=score, reverse=True)
    top = kept[:top_k]
    # 确定是这类菜的够 3 家，就不拿"可能"的来凑数；
    # 不够才补，补进来的会在界面上标"菜系未确认"。
    # 这条规则跟"宁可只给 1 家对的，也不凑 3 家错的"是同一个原则。
    if len(top) < top_k:
        top += unsure[:top_k - len(top)]
    return top, len(kept) + len(unsure)


def retrieve_amap(slots, radius_km=None, top_k=3):
    """走高德：周边搜索 → 硬约束过滤 → 排序。返回 (推荐候选, 范围内总数, 是否放宽, 实际半径)"""
    radius = radius_km or slots.get("max_km") or DEFAULT_RADIUS_KM
    # 范围越大，圈里的店越多，多翻几页才够填满地图（每页 25 家）
    pages = 2 if radius <= 3 else 4
    cands = amap.search(slots.get("category"), CENTER, radius, pages=pages)

    # 一家都没搜到（约束太死或类型太窄）：去掉菜系再搜一轮，并在界面提示已放宽
    if not cands:
        cands = amap.search(None, CENTER, radius, pages=pages)

    top, pool = _amap_filter_rank(cands, slots, top_k)
    relaxed = False
    if not top and cands:
        relaxed = True
        top, pool = _amap_filter_rank(cands, {**slots, "budget": None, "max_km": None}, top_k)
    return top, pool, relaxed, radius, cands


# ---------- 第四步：生成推荐方案 ----------
GEN_PROMPT = """你是本地生活推荐助手。下面是通过筛选的真实商户数据，请推荐其中 3 家（不足 3 家就有几家说几家）。

铁规则：
1. 只能使用下面数据里出现的信息，绝对不许编造菜品、评分、距离、优惠、包间。
2. 每家的推荐理由必须写出"人均X元"和"距您X.X公里"，数字要跟数据完全一致。
   如果该店的 price_estimated 是 true，改写"人均约X元"，不许说成确定价格。
3. 理由控制在 25 个字以内，说清为什么适合用户这次的需求。
4. 最后不要加总结性废话。
5. 候选列表不为空时，items 必须至少给出 1 家，绝对不许返回空数组。
   用户提的某些条件（尤其"要包间"）在这批数据里可能根本没有对应字段，
   这很正常 —— 照常推荐，理由里不提那个条件就行，不要因此拒绝推荐。
6. 条件是"软"的：数据看不出来的项，既不要当成满足来吹，也不要当成不满足来否定。
   
用户需求：{query}
解析出的条件：{slots}

候选商户数据：
{pois}

只输出 JSON：
{{"items": [{{"name": "店名", "reason": "理由"}}]}}"""


_BRACKETS = re.compile(r"[\(（\[【].*?[\)）\]】]")


def _norm_name(s):
    """店名归一化：去掉分店括号、空格、间隔号，全角转半角，方便把模型复述的名字对回去。"""
    s = _BRACKETS.sub("", s or "")
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"[\s·・\-—_]+", "", s)
    return s.lower()


def _find_poi(name, candidates):
    """
    把模型写的店名对回候选数据。

    为什么不能直接用 dict 精确匹配：高德真实店名带长后缀，比如
    "费大厨辣椒炒肉(高德置地·春店)"，模型复述时经常简化成"费大厨辣椒炒肉"，
    精确匹配会全军覆没 → 结果就是"明明有候选却返回空"。这里做三级匹配。
    """
    if not name:
        return None
    for c in candidates:
        if c["name"] == name:
            return c
    n = _norm_name(name)
    if not n:
        return None
    for c in candidates:
        if _norm_name(c["name"]) == n:
            return c
    for c in candidates:
        nc = _norm_name(c["name"])
        if nc and (n in nc or nc in n):
            return c
    return None


def generate_answer(query, slots, candidates):
    brief = [
        {
            "name": c["name"], "category": c["category"], "avg_price": c["avg_price"],
            "price_estimated": c.get("price_estimated", False),
            "rating": c["rating"], "distance_km": c["distance_km"],
            "tags": c["tags"], "signature": c.get("signature", ""),
        }
        for c in candidates
    ]
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": GEN_PROMPT.format(
                query=query,
                slots=json.dumps(slots, ensure_ascii=False),
                pois=json.dumps(brief, ensure_ascii=False, indent=1),
            ),
        }],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content.strip())
    items = data.get("items", [])
    # 把模型写的理由挂回真实数据上，价格和距离以数据为准（不信任模型复述的数字）
    recs, used = [], set()
    for it in items:
        c = _find_poi(it.get("name"), candidates)
        if not c or c["name"] in used:
            continue
        used.add(c["name"])
        recs.append(_pack(c, it.get("reason", "")))

    # 兜底：模型给的名字一个都对不上时，不能让用户看到"附近没有店"这种假结论。
    # 用过滤后的候选按排序补齐，理由用模板生成（数字照样来自真实数据）。
    if not recs:
        for c in candidates[:3]:
            price = c.get("avg_price")
            dist = c.get("distance_km")
            approx = "约" if c.get("price_estimated") else ""
            reason = f"人均{approx}{price}元，距您{dist}公里"
            recs.append(_pack(c, reason))
    return recs


def _pack(c, reason):
    """把候选的真实字段 + 模型写的理由拼成一条推荐。"""
    return {
        "name": c["name"],
        "avg_price": c.get("avg_price"),
        "price_estimated": c.get("price_estimated", False),
        "distance_km": c.get("distance_km"),
        "tags": c.get("tags", []),
        "category": c.get("category", ""),
        "lat": c.get("lat"),
        "lng": c.get("lng"),
        "address": c.get("address", ""),
        "reason": reason,
        # 下面这几个字段是界面用来表达"能核 / 核不了"的，不参与排序
        "open_time": c.get("open_time", ""),
        "open_status": c.get("open_status"),
        "has_room": c.get("has_room"),        # None = 数据源不提供，不是"没有"
        "room_hint": c.get("room_hint", False),
        "tel": c.get("tel", ""),
        "category_unverified": c.get("category_unverified", False),
        "photos": c.get("photos", []),
    }


def _notices(slots, cands, recs=None):
    """
    把"我没能核实的事"明明白白说出来。

    产品上这是最容易被做错的地方：数据缺字段时，最容易的做法是假装它满足了，
    其次是把它当成不满足直接淘汰。两种都在骗用户。
    正确做法是第三条路——保留、标注、把决定权交回用户。
    """
    out = []
    if slots.get("need_room") is True:
        out.append("包间：数据源不提供这个字段，已把「可能有包间」的往前排，到店前请电话确认")
    if slots.get("at_time"):
        unknown = sum(1 for c in cands if c.get("open_status") == "unknown")
        if unknown:
            out.append(f"营业时间：{unknown} 家店查不到营业时间，未做时间校验")
    if any(c.get("price_estimated") for c in cands):
        n = sum(1 for c in cands if c.get("price_estimated"))
        out.append(f"人均：{n} 家店没有真实人均，按菜系估算（标「约」）")
    if slots.get("category") and recs:
        vague = sum(1 for r in recs if r.get("category_unverified"))
        if vague:
            out.append(f"菜系：其中 {vague} 家的高德分类看不出具体菜系，是补位推荐，"
                       f"可能不是你想吃的那类，点开前建议看一眼招牌菜")
    return out


def run(query, radius_km=None, source="local", slots=None):
    """
    一次完整的问答。

    source:
      "local" —— 198 家模拟数据（评测用，结果可复现）
      "amap"  —— 高德真实商户（桌面程序用）
      "auto"  —— 有 key 就走高德，没有就退回本地
    slots: 已经解析好的条件（拖半径滑杆时复用，避免重复调模型）
    """
    slots = slots or parse_query(query)

    if source == "auto":
        source = "amap" if amap.has_key() else "local"

    if source == "amap" and amap.has_key():
        cands, pool, relaxed, radius, all_cands = retrieve_amap(slots, radius_km)
        recs = generate_answer(query, slots, cands)
        top_names = {r["name"] for r in recs}
        return {
            "query": query,
            "slots": slots,
            "recs": recs,
            "pool": pool,
            "relaxed": relaxed,
            "source": "amap",
            "notices": _notices(slots, all_cands, recs),
            "map": {
                "center": CENTER,
                "radius_km": radius,
                "pois": [{
                    "name": c["name"], "lat": c["lat"], "lng": c["lng"],
                    "avg_price": c["avg_price"],
                    "price_estimated": c.get("price_estimated", False),
                    "distance_km": c.get("distance_km"),
                    "rating": c.get("rating"),
                    "category": c.get("category"),
                    "recommended": c["name"] in top_names,
                } for c in all_cands[:60]],
            },
        }

    # ---- 本地模拟数据（原逻辑，不变）----
    if radius_km:
        slots = {**slots, "max_km": radius_km}
    cands, pool, relaxed = retrieve_and_rank(slots)
    recs = generate_answer(query, slots, cands)
    top_names = {r["name"] for r in recs}
    in_ring = [with_distance(p) for p in POIS
               if slots.get("max_km") is None or with_distance(p)["distance_km"] <= slots["max_km"]]
    return {
        "query": query,
        "slots": slots,
        "recs": recs,
        "pool": pool,
        "relaxed": relaxed,
        "source": "local",
        "map": {
            "center": CENTER,
            "radius_km": radius_km or slots.get("max_km") or DEFAULT_RADIUS_KM,
            "pois": [{
                "name": c["name"], "lat": c["lat"], "lng": c["lng"],
                "avg_price": c["avg_price"],
                "price_estimated": False,
                "distance_km": c["distance_km"],
                "rating": c["rating"],
                "category": c["category"],
                "recommended": c["name"] in top_names,
            } for c in sorted(in_ring, key=lambda x: x["distance_km"])[:60]],
        },
    }


if __name__ == "__main__":
    r = run("12个人聚餐，人均120以内，要有包间")
    print(json.dumps(r, ensure_ascii=False, indent=2))
