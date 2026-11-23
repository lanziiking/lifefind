"""
run_demo.py —— 现场演示用：同一句话，同时跑 v1 和 v2，把差异直接摆出来。

面试时你可以打开这个跑一句，比嘴上讲十句都有用。
每一家店后面会自动标出它违反了哪条硬约束（超预算 / 超距离 / 没包间）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent
import baseline
from evaluate import check_rec, POI_BY_NAME


def show(title, recs, exp):
    print(f"\n【{title}】")
    if not recs:
        print("  （没有给出推荐）")
        return
    for i, r in enumerate(recs, 1):
        ok, hallu, notes = check_rec(r["name"], r.get("reason", ""), exp)
        flag = "✓ 合规" if ok and not hallu else "✗ " + "、".join(notes)
        poi = POI_BY_NAME.get(r["name"])
        real = f"真实人均{poi['avg_price']}元/距离{poi['distance_km']}km" if poi else "库中无此店"
        print(f"  {i}. {r['name']}（{real}）")
        print(f"     理由：{r.get('reason', '')}")
        print(f"     → {flag}")


def main():
    print("=" * 60)
    print("本地生活找店助手 · v1 vs v2 对比演示")
    print("输入一句话描述你的需求，回车看结果；直接回车退出")
    print("示例：12个人聚餐，人均120以内，要有包间，3公里内")
    print("=" * 60)

    while True:
        q = input("\n你的需求 > ").strip()
        if not q:
            print("已退出。")
            break

        # v2 会先解析需求，用解析结果判断合规性
        v2 = agent.run(q)
        exp = {k: v2["slots"].get(k) for k in ["budget", "max_km", "need_room", "category"]}
        exp["avoid"] = v2["slots"].get("avoid", [])

        v1 = baseline.run(q)

        print(f"\n解析出的需求：{json.dumps(v2['slots'], ensure_ascii=False)}")
        if v2["relaxed"]:
            print("⚠ 没有完全符合条件的店，已放宽条件（只保留菜系）")
        show("v2 工具版（预算/距离/包间用代码算）", v2["recs"], exp)
        show("v1 纯大模型版（全靠它自己估）", v1["recs"], exp)
        print("\n" + "-" * 60)


if __name__ == "__main__":
    main()
