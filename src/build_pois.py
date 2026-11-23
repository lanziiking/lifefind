"""
build_pois.py —— 把 POI 库从 50 家扩充到 230 家

为什么要扩充：
  第一版只有 50 家店、18 个菜系，平均每个菜系不到 3 家。
  用户说"吃湘菜"，召回后只剩 2 家，再一叠预算和距离就只剩 1 家。
  这时候"排序"根本没起作用——不是算法没用，是供给不够。

  真实场景接高德/美团，同类商户是几十上百家。
  所以这里按菜系密度补足（每菜系约 10 家），让排序环节真正有意义。

随机数用了固定种子，所以每次生成的结果完全一样，可复现。
"""

import json
import math
import random
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
seed = json.loads((BASE_DIR / "data" / "pois_seed.json").read_text(encoding="utf-8"))
USER = seed["_meta"]["user_location"]

random.seed(42)          # 固定种子 = 可复现

# 各菜系的基准人均（元），用来生成合理的价格带
BASE_PRICE = {
    "粤菜": 90, "潮汕菜": 95, "川菜": 75, "湘菜": 78, "火锅": 105,
    "潮汕牛肉火锅": 95, "日料": 120, "日式烤肉": 130, "烧烤": 75,
    "西餐": 120, "东南亚菜": 95, "面食": 45, "海鲜": 160, "素食": 65,
    "西北菜": 85, "江浙菜": 80, "快餐简餐": 35, "甜品咖啡": 40,
}

PREFIX = ["老码头", "小院", "食光", "淳味", "巷子", "三味", "拾味", "半山", "好味",
          "家味", "鲜煮意", "南山", "粤海", "一品", "同福", "得闲", "客味", "禾田",
          "码头", "锦记", "陈记", "李记", "梁记", "阿婆", "大娘家", "十三行"]
SUFFIX = ["餐厅", "小馆", "食府", "饭店", "酒家", "私厨", "食堂", "菜馆"]
TAGS = ["有包间", "可停车", "安静", "适合带娃", "夜宵", "可拼桌", "可开发票", "宠物友好"]
SIGNATURE = {
    "粤菜": ["白切鸡", "烧鹅", "豉油鸡", "煲仔饭"], "潮汕菜": ["卤鹅", "蚝烙", "牛肉丸"],
    "川菜": ["水煮鱼", "回锅肉", "毛血旺"], "湘菜": ["剁椒鱼头", "小炒肉", "腊味合蒸"],
    "火锅": ["牛油锅", "番茄锅", "毛肚"], "潮汕牛肉火锅": ["吊龙", "匙柄", "胸口油"],
    "日料": ["刺身拼盘", "寿司", "天妇罗"], "日式烤肉": ["厚切牛舌", "牛小排"],
    "烧烤": ["烤生蚝", "羊肉串", "烤茄子"], "西餐": ["牛排", "意面", "汉堡"],
    "东南亚菜": ["冬阴功", "咖喱蟹", "河粉"], "面食": ["牛肉面", "云吞面", "手工面"],
    "海鲜": ["清蒸石斑", "蒜蓉扇贝", "椒盐濑尿虾"], "素食": ["罗汉斋", "素鲍鱼"],
    "西北菜": ["大盘鸡", "羊肉泡馍", "莜面"], "江浙菜": ["东坡肉", "盐水鸭", "糖醋排骨"],
    "快餐简餐": ["烧腊饭", "鸡汤", "套餐"], "甜品咖啡": ["拿铁", "双皮奶", "多肉葡萄"],
}

pois = list(seed["pois"])
used_names = {p["name"] for p in pois}
idx = len(pois)


def make_name():
    for _ in range(200):
        n = random.choice(PREFIX) + random.choice(SUFFIX)
        if n not in used_names:
            used_names.add(n)
            return n
    return f"无名小店{idx}"


def jitter(r_km):
    """把一个距离和随机方位，换算成经纬度偏移"""
    theta = random.uniform(0, 2 * math.pi)
    dlat = r_km * math.cos(theta) / 111.0
    dlng = r_km * math.sin(theta) / 102.0
    return USER["lat"] + dlat, USER["lng"] + dlng


for cat, base in BASE_PRICE.items():
    existing = sum(1 for p in pois if p["category"] == cat)
    need = max(0, 11 - existing)          # 每个菜系补到 11 家左右
    for _ in range(need):
        idx += 1
        price = int(round(base * random.uniform(0.6, 1.6) / 5.0) * 5)
        price = max(15, price)
        r = random.uniform(0.3, 8.0)
        lat, lng = jitter(r)
        tags = []
        if random.random() < 0.45:
            tags.append("有包间")
        for t in random.sample(TAGS[1:], k=random.randint(0, 2)):
            if t not in tags:
                tags.append(t)
        pois.append({
            "id": f"G{idx:03d}",
            "name": make_name(),
            "category": cat,
            "avg_price": price,
            "rating": round(random.uniform(3.6, 4.8), 1),
            "lat": round(lat, 4),
            "lng": round(lng, 4),
            "tags": tags,
            "signature": random.choice(SIGNATURE[cat]),
            "district": random.choice(["天河", "越秀", "海珠", "白云"]),
        })

out = {
    "_meta": {
        "说明": "模拟 POI 数据，用于本项目的功能验证与评测，不代表真实商户信息",
        "user_location": USER,
        "count": len(pois),
        "generated_by": "src/build_pois.py（种子 42，可复现）",
    },
    "pois": pois,
}
(BASE_DIR / "data" / "pois.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
)

from collections import Counter
c = Counter(p["category"] for p in pois)
print("总商户数:", len(pois))
print("各菜系数量:", dict(sorted(c.items(), key=lambda x: -x[1])))
print("有包间的比例: %.0f%%" % (sum(1 for p in pois if "有包间" in p["tags"]) / len(pois) * 100))
