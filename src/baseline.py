"""
baseline.py —— 找店助手 v1（纯大模型直答，没有工具、没有约束校验）

这是对照组。做法是很多人第一反应会想到的：
把店铺列表整段塞给大模型，让它自己挑 3 家。

它的问题是结构性的，不是模型不够聪明：
  1. 它看不到坐标，所以"3公里内"只能靠猜 —— 距离本质是个计算问题，不是语言问题
  2. 它要在一屏文字里同时心算预算、距离、包间三个约束，漏掉一个就出错
  3. 它的理由里会出现数据里根本没有的信息（编造），也就是幻觉

这两版跑同一批 80 条 query，差距就是"工具有没有交给代码算"的价值。
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def _base_dir():
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
if getattr(sys, "frozen", False):
    load_dotenv(Path(sys.executable).parent / ".env")
load_dotenv(BASE_DIR / ".env")

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    timeout=45,        # 同 agent.py，避免请求挂住
    max_retries=2,
)
MODEL = "qwen-plus"
DATA = json.loads((BASE_DIR / "data" / "pois.json").read_text(encoding="utf-8"))
POIS = DATA["pois"]

PROMPT = """你是本地生活推荐助手。下面是全部可选商户（只给了店名、菜系、人均、评分、标签，没有坐标）。

请根据用户需求挑出最合适的 3 家。

用户需求：{query}

商户列表：
{pois}

只输出 JSON：
{{"items": [{{"name": "店名", "reason": "推荐理由，25字以内，要说清人均多少钱、大概多远"}}]}}"""


def run(query):
    brief = [
        {"name": p["name"], "category": p["category"], "avg_price": p["avg_price"],
         "rating": p["rating"], "tags": p["tags"]}
        for p in POIS
    ]
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": PROMPT.format(query=query, pois=json.dumps(brief, ensure_ascii=False)),
        }],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content.strip())
    by_name = {p["name"]: p for p in POIS}
    recs = []
    for it in data.get("items", []):
        p = by_name.get(it.get("name"))
        if not p:
            continue
        recs.append({"name": p["name"], "reason": it.get("reason", "")})
    return {"query": query, "recs": recs}


if __name__ == "__main__":
    print(json.dumps(run("12个人聚餐，人均120以内，要有包间"), ensure_ascii=False, indent=2))
