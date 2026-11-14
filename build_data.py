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
TABLES = ["Item", "Recipe", "RecipeLevelTable", "CraftType", "SecretRecipeBook",
          "GatheringItem", "GatheringItemLevelConvertTable", "GatheringPointBase", "GatheringPoint",
          "GatheringPointTransient", "GatheringSubCategory", "TerritoryType", "PlaceName"]
DCS = ["陆行鸟", "莫古力", "猫小胖", "豆豆柴"]
UA = {"User-Agent": "today-craft-profit/1.0"}

# 第一轮粗筛的宽松门槛（用汇总价格，可能有误差）
COARSE_RATIO = 0.6
# 给网页留的余量：复查后成本占售价 50% 以内的都写进数据，网页上再按门槛筛
KEEP_RATIO = 0.5
MARKET_TAX = 0.05  # 市场交易税（只用于显示“到手价”）
PLAN_CRAFTS = 3    # 按“做 3 次”的量去算材料要买多少，避免被单个超低价挂单误导
HISTORY_DAYS = 15  # 只看最近 15 天的成交（售价中位数和每天卖出数量都按这个算）
STATS_MS = HISTORY_DAYS * 86400000

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


def fetch_detail(dc, ids, step=10):
    """查询挂单明细和最近成交记录（dc 也可以是单个服的名字）。返回 {物品ID: 明细}"""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    ids = sorted(ids)
    enc = urllib.parse.quote(dc)
    # 大区明细查询很慢，一次查太多会超时；单个服快得多，可以一次多查一些
    chunks = [ids[i:i + step] for i in range(0, len(ids), step)]

    def one(chunk):
        url = "https://universalis.app/api/v2/%s/%s?listings=40&entries=30&statsWithin=%d&fields=%s" % (
            enc, ",".join(map(str, chunk)), STATS_MS, DETAIL_FIELDS)
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


def dc_worlds(dc):
    """返回这个大区下面所有服的名字"""
    names = {w["id"]: w["name"] for w in http_json("https://universalis.app/api/v2/worlds")}
    for d in http_json("https://universalis.app/api/v2/data-centers"):
        if d.get("name") == dc:
            return [names[i] for i in d.get("worlds", []) if i in names]
    return []


def world_sell(world, items_hq):
    """
    查一个服里成品/采集物的卖价和销量。
    items_hq: {物品ID: 是否优先按 HQ 算}
    返回 {物品ID字符串: [售价, 是否HQ, 每天卖出, 最近成交时间, 在卖数量, 挂单是否满, 数据更新时间]}
    在这个服最近 15 天成交少于 2 次的物品不写（说明这个服基本没人买）。
    """
    D = fetch_detail(world, list(items_hq), step=25)
    out = {}
    for iid, hq in items_hq.items():
        sell = real_sell(D.get(iid), hq)
        if not sell:
            continue
        out[str(iid)] = [round(sell["price"]), 1 if sell["q"] == "hq" else 0, round(sell["vel"], 2),
                         sell["lastSale"], sell["onSale"], 1 if sell["capped"] else 0,
                         (D.get(iid) or {}).get("lastUploadTime")]
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
            # 网站最多返回 40 条挂单；满 40 条说明实际在卖的更多
            "capped": len(detail.get("listings") or []) >= 40,
            "sales": len(hs),
        }
    return None


def craft_detail_ids(rows):
    ids = set()
    for x in rows:
        ids.add(x["item"])
        ids.update(i for i, _, _ in x["ings"])
    return ids


def refine(dc, rows, items, recipes_by_id, D):
    """第二轮：对候选配方用挂单明细和成交记录重算（D = fetch_detail 的结果）"""
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


# ======================== 采集（今天挖什么） ========================

# GatheringPoint.Type：1 = 普通采集点；2 = 限时（未知/稀有）；4、5 = 传说 / 限时传说
NORMAL_POINT = {"1"}
TIMED_POINT = {"2", "4", "5"}
GATHER_JOB = {"0": "采矿", "1": "采矿", "2": "园艺", "3": "园艺"}
GATHER_MIN_PRICE = 30   # 太便宜的不看


def load_gathering(items):
    """返回 {物品ID: 采集信息}，只包含能在市场交易的采矿/园艺物品"""
    def rd(name):
        return read_table(name)

    gh, gi = rd("GatheringItem")
    lvh, lv = rd("GatheringItemLevelConvertTable")
    lvl = {int(r[0]): (int(r[lvh.index("GatheringItemLevel")]), int(r[lvh.index("Stars")])) for r in lv}
    # GatheringItem 行 ID → (物品ID, 等级, 星级, 是否隐藏)
    gitem = {}
    for r in gi:
        iid = int(r[gh.index("Item")])
        if iid <= 0 or not items.get(iid, {}).get("marketable"):
            continue
        l, st = lvl.get(int(r[gh.index("GatheringItemLevel")]), (0, 0))
        gitem[int(r[0])] = (iid, l, st, False)  # 国服表的“隐藏”列位置不可靠，暂不使用

    bh, base = rd("GatheringPointBase")
    bases = {}
    for r in base:
        t = r[bh.index("GatheringType")]
        if t not in GATHER_JOB:
            continue
        slots = [int(r[bh.index("Item[%d]" % k)]) for k in range(8)]
        bases[int(r[0])] = (GATHER_JOB[t], [s for s in slots if s in gitem])

    th, tr = rd("GatheringPointTransient")
    timed_ids = {int(r[0]) for r in tr
                 if r[th.index("EphemeralStartTime")] not in ("65535", "0")
                 or r[th.index("GatheringRarePopTimeTable")] != "0"}

    sh, sub = rd("GatheringSubCategory")
    folklore = {int(r[0]): r[sh.index("FolkloreBook")] for r in sub if r[sh.index("FolkloreBook")]}

    ph, pl = rd("PlaceName")
    place = {int(r[0]): r[ph.index("Name")] for r in pl}
    tth, tt = rd("TerritoryType")
    zone = {int(r[0]): place.get(int(r[tth.index("PlaceName")]), "") for r in tt}

    ph2, pts = rd("GatheringPoint")
    # 国服数据表的列名比实际数据少了一列（Type 后面多了一个 True/False 列），按实际位置读
    c_type = ph2.index("Type")
    shift = 1 if pts and pts[0][ph2.index("GatheringPointBase")] in ("True", "False") else 0
    c_base = ph2.index("GatheringPointBase") + shift
    c_terr = ph2.index("TerritoryType") + shift
    c_sub = ph2.index("GatheringSubCategory") + shift
    out = {}
    for r in pts:
        ptype = r[c_type]
        if ptype not in NORMAL_POINT and ptype not in TIMED_POINT:
            continue
        b = bases.get(int(r[c_base]))
        if not b:
            continue
        job, slots = b
        timed = ptype in TIMED_POINT or int(r[0]) in timed_ids
        book = folklore.get(int(r[c_sub]), "")
        z = zone.get(int(r[c_terr]), "")
        for s in slots:
            iid, l, st, hidden = gitem[s]
            g = out.setdefault(iid, {"jobs": set(), "lv": l, "st": st, "hidden": hidden,
                                     "easy": False, "timed": False, "book": "", "zones": set(), "easyZones": set()})
            g["jobs"].add(job)
            if timed or book:
                g["timed"] = g["timed"] or timed
                g["book"] = g["book"] or book
            else:
                g["easy"] = True  # 至少有一个普通采集点能挖到
                if z:
                    g["easyZones"].add(z)
            if z:
                g["zones"].add(z)
    return out


def gather_candidates(gather, market):
    """第一轮粗筛：用汇总数据，挑出普通品质卖得动、价格不太低的"""
    ids = []
    for iid in gather:
        s = summarize(market.get(iid))
        if not s:
            continue
        nq = s["nq"]
        if nq["vel"] > 0 and (nq["avg"] or 0) >= GATHER_MIN_PRICE:
            ids.append(iid)
    return ids


def build_gather(ids, gather, D):
    """第二轮：用成交记录算出可靠售价"""
    out = []
    for iid in ids:
        sell = real_sell(D.get(iid), False)  # 采集出来基本都是普通品质
        if not sell or sell["price"] < GATHER_MIN_PRICE:
            continue
        g = gather[iid]
        zones = sorted(g["easyZones"] or g["zones"])
        out.append({
            "item": iid,
            "jobs": sorted(g["jobs"]),
            "lv": g["lv"],
            "st": g["st"],
            "easy": g["easy"],          # 有普通采集点
            "timed": g["timed"] and not g["easy"],
            "book": g["book"] if not g["easy"] else "",
            "hidden": g["hidden"],
            "zones": zones[:3],
            "zoneMore": max(0, len(zones) - 3),
            "price": round(sell["price"]),
            "vel": round(sell["vel"], 2),
            "lastSale": sell["lastSale"],
            "onSale": sell["onSale"],
            "capped": sell["capped"],
            "sales": sell["sales"],
            "upd": (D.get(iid) or {}).get("lastUploadTime"),
        })
    out.sort(key=lambda x: -(x["price"] * x["vel"]))
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
    gather = load_gathering(items)
    ids |= set(gather)
    log("可交易采集物 %d 种；一共要查 %d 种物品" % (len(gather), len(ids)))

    os.makedirs(OUT, exist_ok=True)
    dcs = sys.argv[1:] or DCS
    for dc in dcs:
        log("查询大区", dc)
        market = fetch_market(dc, ids)
        rows = build_dc(dc, items, recipes, market)
        gids = gather_candidates(gather, market)
        detail_ids = craft_detail_ids(rows) | set(gids)
        log("  %s: 复查 %d 个配方 + %d 种采集物，涉及物品 %d 种" % (dc, len(rows), len(gids), len(detail_ids)))
        D = fetch_detail(dc, detail_ids)
        rows = refine(dc, rows, items, recipes_by_id, D)
        grows = build_gather(gids, gather, D)
        # 物品名直接放进每个大区的文件里，几个大区可以同时跑、互不影响
        names = {}
        for x in rows:
            names[str(x["item"])] = items[x["item"]]["name"]
            for iid, _, _ in x["ings"]:
                names[str(iid)] = items[iid]["name"]
        for x in grows:
            names[str(x["item"])] = items[x["item"]]["name"]
        # 按单个服再查一遍成品和采集物的卖价、销量（买材料可以跨服，所以材料成本还是按整个大区算）
        items_hq = {}
        for x in rows:
            r = recipes_by_id[x["r"]]
            items_hq[x["item"]] = items_hq.get(x["item"], False) or (r["canHq"] and items[x["item"]]["canHq"])
        for x in grows:
            items_hq.setdefault(x["item"], False)
        worlds, per_world = dc_worlds(dc), {}
        for w in worlds:
            try:
                per_world[w] = world_sell(w, items_hq)
                log("  %s / %s: %d 种有成交" % (dc, w, len(per_world[w])))
            except Exception as e:  # 某个服查失败不影响整个大区
                log("  %s / %s 查询失败: %s" % (dc, w, e))
        write_out(dc, {"dc": dc, "generated": int(time.time() * 1000), "names": names,
                       "days": HISTORY_DAYS, "worlds": [w for w in worlds if w in per_world],
                       "world": per_world, "rows": rows, "gather": grows}, "CRAFT_DATA", dc)
        log("  %s: 写出 %d 个配方、%d 种采集物" % (dc, len(rows), len(grows)))
    log("完成", datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
