"""
「今天搓什么能赚钱」数据生成程序

做的事：
1. 读取国服游戏数据（配方表、物品表）
2. 向 Universalis 查询四个大区所有相关物品的价格和销量
3. 算出每个配方的材料成本、成品售价、利润，写成 data/<大区>.json 给网页用

用法：  python build_data.py
（第一次运行会自动下载游戏数据到 gamedata/ 文件夹）
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GAMEDATA = os.path.join(HERE, "gamedata")
OUT = os.path.join(HERE, "data")

DATA_REPO = "https://raw.githubusercontent.com/thewakingsands/ffxiv-datamining-cn/master/"
TABLES = ["Item", "Recipe", "RecipeLevelTable", "CraftType", "SecretRecipeBook"]
DCS = ["陆行鸟", "莫古力", "猫小胖", "豆豆柴"]
UA = {"User-Agent": "today-craft-profit/1.0"}

# 第一轮粗筛的宽松门槛（用汇总价格，可能有误差）
COARSE_RATIO = 0.6
# 给网页留的余量：复查后成本占售价 50% 以内的都写进数据，网页上再按门槛筛
KEEP_RATIO = 0.5
MARKET_TAX = 0.05  # 市场交易税（只用于显示“到手价”）
PLAN_CRAFTS = 3    # 按“做 3 次”的量去算材料要买多少，避免被单个超低价挂单误导
HISTORY_DAYS = 30  # 只看最近 30 天的成交

DETAIL_FIELDS = ",".join([
    "items.itemID", "items.lastUploadTime", "items.nqSaleVelocity", "items.hqSaleVelocity",
    "items.listings.pricePerUnit", "items.listings.quantity", "items.listings.hq",
    "items.recentHistory.pricePerUnit", "items.recentHistory.quantity",
    "items.recentHistory.hq", "items.recentHistory.timestamp",
])


try:  # Windows 命令行默认编码打不出中文
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(*a):
    print(*a, flush=True)


def http_json(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # 网络抖动 / 限流 / 网站超时时稍等重试
            if i == tries - 1:
                raise
            log("  重试:", e)
            time.sleep(3 + i * 5)


def ensure_gamedata():
    os.makedirs(GAMEDATA, exist_ok=True)
    for t in TABLES:
        p = os.path.join(GAMEDATA, t + ".csv")
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            log("下载游戏数据", t)
            req = urllib.request.Request(DATA_REPO + t + ".csv", headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r, open(p, "wb") as f:
                f.write(r.read())


def read_table(name):
    """返回 (表头列名列表, 数据行列表)。数据行第 0 列是 ID。"""
    with open(os.path.join(GAMEDATA, name + ".csv"), encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header = rows[1]
    data = [r for r in rows[3:] if r and r[0].lstrip("-").isdigit()]
    return header, data


def load_game():
    h, rows = read_table("Item")
    ni, hq, untrade, cat = h.index("Name"), h.index("CanBeHq"), h.index("IsUntradable"), h.index("ItemSearchCategory")
    items = {}
    for r in rows:
        iid = int(r[0])
        items[iid] = {
            "name": r[ni],
            "canHq": r[hq] == "True",
            "marketable": r[cat] not in ("0", "") and r[untrade] != "True",
        }

    _, lv = read_table("RecipeLevelTable")
    lvh, _ = read_table("RecipeLevelTable")
    lvl_col = lvh.index("ClassJobLevel")
    stars_col = lvh.index("Stars")
    levels = {int(r[0]): (int(r[lvl_col]), int(r[stars_col])) for r in lv}

    ch, cr = read_table("CraftType")
    crafts = {int(r[0]): r[ch.index("Name")] for r in cr}

    bh, br = read_table("SecretRecipeBook")
    books = {int(r[0]): r[bh.index("Name")] for r in br}

    h, rows = read_table("Recipe")
    col = {name: h.index(name) for name in
           ["CraftType", "RecipeLevelTable", "Item{Result}", "Amount{Result}", "SecretRecipeBook",
            "IsSpecializationRequired", "IsExpert", "CanHq"]}
    recipes = []
    for r in rows:
        result = int(r[col["Item{Result}"]])
        if result <= 0 or result not in items:
            continue
        ings = []
        for k in range(8):
            iid = int(r[h.index("Item{Ingredient}[%d]" % k)])
            amt = int(r[h.index("Amount{Ingredient}[%d]" % k)])
            if iid > 0 and amt > 0:
                ings.append((iid, amt))
        if not ings:
            continue
        lvl, stars = levels.get(int(r[col["RecipeLevelTable"]]), (0, 0))
        recipes.append({
            "id": int(r[0]),
            "result": result,
            "amount": max(1, int(r[col["Amount{Result}"]])),
            "ings": ings,
            "job": crafts.get(int(r[col["CraftType"]]), "?"),
            "level": lvl,
            "stars": stars,
            "book": books.get(int(r[col["SecretRecipeBook"]]), "") if r[col["SecretRecipeBook"]] not in ("0", "") else "",
            "specialist": r[col["IsSpecializationRequired"]] == "True",
            "expert": r[col["IsExpert"]] == "True",
            "canHq": r[col["CanHq"]] == "True",
        })
    return items, recipes


def fetch_market(dc, ids):
    """返回 {物品ID: 价格数据}"""
    out = {}
    ids = sorted(ids)
    enc = urllib.parse.quote(dc)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        url = "https://universalis.app/api/v2/aggregated/%s/%s" % (enc, ",".join(map(str, chunk)))
        d = http_json(url)
        for res in d.get("results", []):
            out[res["itemId"]] = res
        if (i // 100) % 20 == 0:
            log("  %s: %d / %d" % (dc, min(i + 100, len(ids)), len(ids)))
        time.sleep(0.15)  # 对网站客气一点
    return out


def fetch_detail(dc, ids):
    """查询挂单明细和最近成交记录。返回 {物品ID: 明细}"""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    ids = sorted(ids)
    enc = urllib.parse.quote(dc)
    step = 10  # 明细查询很慢，一次查太多会超时
    chunks = [ids[i:i + step] for i in range(0, len(ids), step)]

    def one(chunk):
        url = "https://universalis.app/api/v2/%s/%s?listings=40&entries=30&fields=%s" % (
            enc, ",".join(map(str, chunk)), DETAIL_FIELDS)
        d = http_json(url)
        time.sleep(0.2)
        return d.get("items") if len(chunk) > 1 else {str(chunk[0]): d}

    with ThreadPoolExecutor(max_workers=2) as pool:  # 同时最多 2 个请求，对网站客气一点
        for n, items in enumerate(pool.map(one, chunks), 1):
            for k, v in (items or {}).items():
                out[int(k)] = v
            if n % 40 == 0:
                log("  %s 复查: %d / %d" % (dc, min(n * step, len(ids)), len(ids)))
    return out


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def real_buy_cost(detail, need):
    """按挂单从便宜到贵买够 need 个，返回平均单价；挂单不够就返回 None"""
    if not detail:
        return None
    ls = sorted(detail.get("listings") or [], key=lambda x: x["pricePerUnit"])
    got, total = 0, 0
    for x in ls:
        take = min(x["quantity"], need - got)
        got += take
        total += take * x["pricePerUnit"]
        if got >= need:
            return total / need
    return None


def real_sell(detail, prefer_hq):
    """
    成品售价（保守）：最近成交价的中位数；如果现在有人挂得更便宜，按更便宜的算。
    返回 dict 或 None
    """
    if not detail:
        return None
    cutoff = time.time() - HISTORY_DAYS * 86400
    hist = [h for h in (detail.get("recentHistory") or []) if h.get("timestamp", 0) >= cutoff]
    for hq in ((True, False) if prefer_hq else (False,)):
        hs = [h for h in hist if bool(h.get("hq")) == hq]
        if len(hs) < 2:  # 成交太少不可信
            continue
        med = median([h["pricePerUnit"] for h in hs])
        listed = [x for x in (detail.get("listings") or []) if bool(x.get("hq")) == hq]
        low = min((x["pricePerUnit"] for x in listed), default=None)
        price = min(med, low) if low else med
        vel = detail.get("hqSaleVelocity" if hq else "nqSaleVelocity") or 0
        return {
            "price": price,
            "q": "hq" if hq else "nq",
            "vel": vel,
            "lastSale": max(h["timestamp"] for h in hs) * 1000,
            "onSale": sum(x["quantity"] for x in listed),
            "sales": len(hs),
        }
    return None


def refine(dc, rows, items, recipes_by_id):
    """第二轮：对候选配方用挂单明细和成交记录重算"""
    ids = set()
    for x in rows:
        ids.add(x["item"])
        ids.update(i for i, _, _ in x["ings"])
    log("  %s: 复查 %d 个候选，涉及物品 %d 种" % (dc, len(rows), len(ids)))
    D = fetch_detail(dc, ids)
    out = []
    for x in rows:
        r = recipes_by_id[x["r"]]
        sell = real_sell(D.get(r["result"]), r["canHq"] and items[r["result"]]["canHq"])
        if not sell:
            continue
        cost, ings, ok = 0, [], True
        for iid, amt in r["ings"]:
            p = real_buy_cost(D.get(iid), amt * PLAN_CRAFTS)
            if p is None:  # 挂单数量不够，买不齐
                ok = False
                break
            cost += p * amt
            ings.append([iid, amt, round(p)])
        if not ok:
            continue
        unit_cost = cost / r["amount"]
        ratio = unit_cost / sell["price"]
        if ratio > KEEP_RATIO:
            continue
        x.update({
            "q": sell["q"],
            "price": round(sell["price"]),
            "cost": round(unit_cost),
            "ratio": round(ratio, 4),
            "profit": round(sell["price"] * (1 - MARKET_TAX) - unit_cost),
            "vel": round(sell["vel"], 2),
            "lastSale": sell["lastSale"],
            "onSale": sell["onSale"],
            "sales": sell["sales"],
            "upd": (D.get(r["result"]) or {}).get("lastUploadTime") or x["upd"],
            "listed": sell["onSale"] > 0,
            "ings": ings,
        })
        out.append(x)
    out.sort(key=lambda x: -x["profit"])
    return out


def g(d, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def summarize(m):
    """把一个物品的原始价格数据整理成网页要用的几个数"""
    if not m:
        return None
    s = {}
    for q in ("nq", "hq"):
        s[q] = {
            "min": g(m, q, "minListing", "dc", "price"),
            "avg": g(m, q, "averageSalePrice", "dc", "price"),
            "vel": g(m, q, "dailySaleVelocity", "dc", "quantity") or 0,
            "last": g(m, q, "recentPurchase", "dc", "price"),
            "lastAt": g(m, q, "recentPurchase", "dc", "timestamp"),
        }
    ts = [w.get("timestamp", 0) for w in m.get("worldUploadTimes", [])]
    s["updated"] = max(ts) if ts else None
    return s


def buy_price(s):
    """材料买入价：大区里最便宜的挂单（普通或高品质都行）"""
    if not s:
        return None
    c = [p for p in (s["nq"]["min"], s["hq"]["min"]) if p]
    return min(c) if c else None


def sell_price(s, prefer_hq):
    """
    成品预计售价（偏保守）：
    - 用平均成交价；如果现在有人挂得更便宜，就按更便宜的算（你得跟着降价才卖得掉）
    - 能做高品质的东西按高品质算，没有高品质数据时退回普通
    返回 (单价, 品质, 每天卖出数量)
    """
    order = ("hq", "nq") if prefer_hq else ("nq",)
    for q in order:
        x = s[q]
        if not x["avg"] or x["vel"] <= 0:
            continue
        price = x["avg"]
        if x["min"]:
            price = min(price, x["min"])
        return price, q, x["vel"]
    return None, None, 0


def build_dc(dc, items, recipes, market):
    S = {iid: summarize(m) for iid, m in market.items()}
    out = []
    for r in recipes:
        res = S.get(r["result"])
        if not res:
            continue
        price, q, vel = sell_price(res, r["canHq"] and items[r["result"]]["canHq"])
        if not price:
            continue
        cost = 0
        ing_out = []
        ok = True
        for iid, amt in r["ings"]:
            p = buy_price(S.get(iid))
            if p is None:  # 有材料在市场上买不到
                ok = False
                break
            cost += p * amt
            ing_out.append([iid, amt, round(p)])
        if not ok:
            continue
        unit_cost = cost / r["amount"]
        ratio = unit_cost / price
        if ratio > COARSE_RATIO:
            continue
        out.append({
            "r": r["id"],
            "item": r["result"],
            "n": r["amount"],
            "job": r["job"],
            "lv": r["level"],
            "st": r["stars"],
            "book": r["book"],
            "sp": r["specialist"],
            "ex": r["expert"],
            "q": q,
            "price": round(price),
            "cost": round(unit_cost),
            "ratio": round(ratio, 4),
            "profit": round(price * (1 - MARKET_TAX) - unit_cost),
            "vel": round(vel, 2),
            "upd": res["updated"],
            "listed": res[q]["min"] is not None,
            "ings": ing_out,
        })
    out.sort(key=lambda x: -x["profit"])
    return out


def write_out(base, obj, js_var, js_key=None):
    """
    同时写 .json 和 .js 两份。
    .js 版本让网页直接双击打开（file://）时也能加载，不需要开网页服务器。
    """
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, base + ".json"), "w", encoding="utf-8") as f:
        f.write(text)
    target = "window.%s" % js_var if js_key is None else "(window.%s=window.%s||{})[%s]" % (
        js_var, js_var, json.dumps(js_key, ensure_ascii=False))
    with open(os.path.join(OUT, base + ".js"), "w", encoding="utf-8") as f:
        f.write("%s=%s;\n" % (target, text))


def main():
    ensure_gamedata()
    items, recipes = load_game()
    # 只要成品和所有材料都能在市场交易的配方
    recipes = [r for r in recipes
               if items[r["result"]]["marketable"] and all(items.get(i, {}).get("marketable") for i, _ in r["ings"])]
    ids = set()
    for r in recipes:
        ids.add(r["result"])
        ids.update(i for i, _ in r["ings"])
    log("可交易配方 %d 个，涉及物品 %d 种" % (len(recipes), len(ids)))
    recipes_by_id = {r["id"]: r for r in recipes}

    os.makedirs(OUT, exist_ok=True)
    dcs = sys.argv[1:] or DCS
    for dc in dcs:
        log("查询大区", dc)
        market = fetch_market(dc, ids)
        rows = build_dc(dc, items, recipes, market)
        rows = refine(dc, rows, items, recipes_by_id)
        # 物品名直接放进每个大区的文件里，几个大区可以同时跑、互不影响
        names = {}
        for x in rows:
            names[str(x["item"])] = items[x["item"]]["name"]
            for iid, _, _ in x["ings"]:
                names[str(iid)] = items[iid]["name"]
        write_out(dc, {"dc": dc, "generated": int(time.time() * 1000), "names": names, "rows": rows},
                  "CRAFT_DATA", dc)
        log("  %s: 写出 %d 个候选（成本 ≤ 售价 %d%%）" % (dc, len(rows), KEEP_RATIO * 100))
    log("完成", datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
