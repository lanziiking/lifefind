"""
evaluate.py —— 跑 80 条评测集，对比 v1（无工具）和 v2（工具+约束校验）

为什么指标都用【规则】判定，而不让大模型自己打分？
因为大模型打分不可复现：同一份结果跑两次可能给出不同分数，
面试官问"你这个数怎么来的"，你没法自证。规则判定可以一行行讲清楚。

四个指标：
  1. 槽位抽取准确率  —— 需求有没有听对（6 个字段逐个比）
  2. 硬约束满足率    —— 推荐的店有没有超预算/超距离/没包间（用真实数据核对）
  3. 幻觉率          —— 理由里有没有编造包间、价格、距离
  4. 方案可用率      —— 一条 query 的推荐里至少 2 家完全合规，才算"能用"
"""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent
import baseline

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) \
    if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
EVAL = json.loads((BASE_DIR / "data" / "eval_queries.json").read_text(encoding="utf-8"))
QUERIES = EVAL["queries"]

POI_BY_NAME = {p["name"]: agent.with_distance(p) for p in agent.POIS}
FIELDS = ["people", "budget", "max_km", "need_room", "category", "avoid"]

PRICE_RE = re.compile(r"(?:人均|平均)\s*(\d+)\s*元|(\d+)\s*元\s*/?\s*人")
DIST_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:公里|km|KM)")


# ---------- 指标1：槽位抽取 ----------
def slot_correct(pred, exp, field):
    p, e = pred.get(field), exp.get(field)
    if field == "avoid":
        return sorted(p or []) == sorted(e or [])
    if field == "max_km":
        if e is None or p is None:
            return e is None and p is None
        try:
            return abs(float(p) - float(e)) <= 0.01
        except (TypeError, ValueError):
            return False
    if field == "need_room":
        return p is e or p == e
    return p == e


# ---------- 指标2/3：硬约束与幻觉 ----------
def check_rec(name, reason, exp):
    """返回 (是否满足全部硬约束, 是否有幻觉, 违规说明列表)"""
    poi = POI_BY_NAME.get(name)
    if poi is None:                     # 推荐了库里没有的店，直接算失败
        return False, True, ["推荐了不存在的店"]

    bad, hallu = [], []

    # 硬约束：预算
    if exp.get("budget") is not None and poi["avg_price"] > exp["budget"]:
        bad.append(f"超预算({poi['avg_price']}>{exp['budget']})")
    # 硬约束：距离
    if exp.get("max_km") is not None and poi["distance_km"] > exp["max_km"]:
        bad.append(f"超距离({poi['distance_km']}>{exp['max_km']}km)")
    # 硬约束：包间
    if exp.get("need_room") is True and "有包间" not in poi["tags"]:
        bad.append("无包间")

    # 幻觉：理由里说有包间，但数据里没有
    if "包间" in reason and "有包间" not in poi["tags"]:
        hallu.append("虚构包间")
    if "包厢" in reason and "有包间" not in poi["tags"]:
        hallu.append("虚构包厢")

    # 幻觉：理由里的价格与真实人均不符
    m = PRICE_RE.search(reason)
    if m:
        claimed = int(m.group(1) or m.group(2))
        if abs(claimed - poi["avg_price"]) > 1:
            hallu.append(f"价格说错({claimed}≠{poi['avg_price']})")

    # 幻觉：理由里的距离与真实距离不符（超过 0.5km 算错）
    m = DIST_RE.search(reason)
    if m:
        claimed = float(m.group(1))
        if abs(claimed - poi["distance_km"]) > 0.5:
            hallu.append(f"距离说错({claimed}≠{poi['distance_km']}km)")

    return len(bad) == 0, len(hallu) > 0, bad + hallu


def run_one(item, version):
    q, exp = item["query"], item["expect"]
    try:
        if version == "v2":
            out = agent.run(q)
            pred_slots = out["slots"]
        else:
            out = baseline.run(q)
            pred_slots = None
        return {"id": item["id"], "query": q, "expect": exp,
                "slots": pred_slots, "recs": out["recs"], "error": None}
    except Exception as e:
        return {"id": item["id"], "query": q, "expect": exp,
                "slots": None, "recs": [], "error": f"{type(e).__name__}: {e}"}


def evaluate(version, workers=2):
    """
    跑一批 query。结果边跑边写到 data/run_{version}.jsonl，
    中途断了重跑会自动跳过已完成的，不用从头再来。
    """
    cache = BASE_DIR / "data" / f"run_{version}.jsonl"
    done = {}
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error"):
                    done[r["id"]] = r

    todo = [q for q in QUERIES if q["id"] not in done]
    print(f"  [{version}] 已完成 {len(done)} 条，本次待跑 {len(todo)} 条", flush=True)

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex, \
                open(cache, "a", encoding="utf-8") as f:
            futures = {ex.submit(run_one, it, version): it for it in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                r = fut.result()
                if r.get("error"):
                    continue                 # 失败的不写缓存，下轮重试
                done[r["id"]] = r
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                print(f"  [{version}] {i}/{len(todo)} {r['id']} ok", flush=True)

    rows = [done[q["id"]] for q in QUERIES if q["id"] in done]

    field_hit = {f: 0 for f in FIELDS}
    all_slot_ok = 0
    rec_total = rec_pass = rec_hallu = 0
    usable = 0
    details = []

    for r in rows:
        exp = r["expect"]

        # 槽位
        if r["slots"] is not None:
            hits = [slot_correct(r["slots"], exp, f) for f in FIELDS]
            for f, h in zip(FIELDS, hits):
                field_hit[f] += int(h)
            all_slot_ok += int(all(hits))

        # 推荐结果
        n_ok = 0
        rec_detail = []
        for rec in r["recs"]:
            ok, hallu, notes = check_rec(rec["name"], rec.get("reason", ""), exp)
            rec_total += 1
            rec_pass += int(ok)
            rec_hallu += int(hallu)
            n_ok += int(ok)
            rec_detail.append({"name": rec["name"], "reason": rec.get("reason", ""),
                               "ok": ok, "notes": notes})
        if r["recs"] and n_ok >= 2:
            usable += 1

        details.append({**r, "rec_detail": rec_detail, "n_ok": n_ok})

    n = len(rows) or 1
    return {
        "version": version,
        "n_queries": len(rows),
        "errors": len(QUERIES) - len(rows),
        "slot_field_acc": {f: round(field_hit[f] / n * 100, 1) for f in FIELDS},
        "slot_all_correct": round(all_slot_ok / n * 100, 1),
        "hard_constraint_pass": round(rec_pass / rec_total * 100, 1) if rec_total else 0,
        "hallucination_rate": round(rec_hallu / rec_total * 100, 1) if rec_total else 0,
        "usable_rate": round(usable / n * 100, 1),
        "avg_recs": round(rec_total / n, 2),
        "details": details,
    }


if __name__ == "__main__":
    print("跑 v2（工具版）...", flush=True)
    v2 = evaluate("v2")
    print("跑 v1（纯大模型版）...", flush=True)
    v1 = evaluate("v1")

    report = {"v2": v2, "v1": v1}
    out_path = BASE_DIR / "data" / "eval_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    def row(label, a, b):
        print(f"{label:<22}{b:>10}{a:>10}")

    print("\n" + "=" * 46)
    print(f"{'指标':<20}{'v1 纯大模型':>12}{'v2 工具版':>12}")
    print("-" * 46)
    row("槽位全字段正确率", v2["slot_all_correct"], "—")
    for f in FIELDS:
        print(f"  · {f:<16}{'—':>12}{v2['slot_field_acc'][f]:>12}")
    row("硬约束满足率 %", v2["hard_constraint_pass"], v1["hard_constraint_pass"])
    row("幻觉率 %", v2["hallucination_rate"], v1["hallucination_rate"])
    row("方案可用率 %", v2["usable_rate"], v1["usable_rate"])
    row("平均推荐店数", v2["avg_recs"], v1["avg_recs"])
    print("=" * 46)
    print("v2 错误条数:", v2["errors"], "| v1 错误条数:", v1["errors"])
    print("详细结果已写入:", out_path)
