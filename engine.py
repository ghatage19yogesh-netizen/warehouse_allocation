"""
Allocation engine: a simplified, self-contained version of the COM allocation
logic (optimization.py + alloc_solver_v2.py), using sample data and SciPy's
HiGHS solver instead of Gurobi.

Flow (same as the original):
  1. Place special products by rule (slow sellers, no-demand products).
  2. Reduce warehouse space by what the rules used.
  3. Solve a linear program for everything else:
       minimise  shipping cost + overflow penalty
       subject to demand met, min/max share per warehouse,
                  space per warehouse x storage type (overflow allowed at a penalty),
                  business exceptions and medium-velocity building limits.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import linprog

WAREHOUSES = ["East", "Central", "West"]
ZONES = ["Northeast", "Southeast", "Midwest", "West"]
STORAGE = ["Folded", "Hanging"]

# Shipping cost per unit ($), warehouse -> zone
DEFAULT_COSTS = pd.DataFrame(
    [[2.00, 3.00, 3.50, 6.00],
     [3.50, 2.50, 2.00, 4.50],
     [6.00, 5.00, 4.00, 2.00]],
    index=WAREHOUSES, columns=ZONES,
)

DEFAULT_CAPACITY = pd.DataFrame(
    {"Warehouse": WAREHOUSES, "Folded": [6000, 5000, 4000], "Hanging": [3000, 2000, 2000]}
)

DEFAULT_SHARES = pd.DataFrame(
    {"Warehouse": WAREHOUSES, "Min %": [20, 20, 20], "Max %": [50, 50, 50]}
)

# velocity: high / medium / low / none ;  allowed = warehouses the product may use
PRODUCTS = [
    {"Product": "Men's Shirts",   "Velocity": "High",   "Storage": "Folded",
     "Demand": [1800, 1200, 1000, 1500], "Allowed": WAREHOUSES, "Rule": ""},
    {"Product": "Women's Denim",  "Velocity": "High",   "Storage": "Folded",
     "Demand": [1200, 900, 800, 1100],   "Allowed": WAREHOUSES, "Rule": ""},
    {"Product": "Fragrance",      "Velocity": "High",   "Storage": "Folded",
     "Demand": [400, 300, 200, 100],     "Allowed": ["East", "Central"],
     "Rule": "Not allowed in West (business exception)"},
    {"Product": "Dresses",        "Velocity": "Medium", "Storage": "Hanging",
     "Demand": [900, 600, 500, 1000],    "Allowed": ["East", "West"],
     "Rule": "Medium velocity: max 2 warehouses"},
    {"Product": "Outerwear",      "Velocity": "Medium", "Storage": "Hanging",
     "Demand": [700, 400, 500, 400],     "Allowed": ["East", "Central"],
     "Rule": "Medium velocity: max 2 warehouses"},
    {"Product": "Silk Scarves",   "Velocity": "Low",    "Storage": "Folded",
     "Demand": [120, 60, 60, 60],        "Allowed": WAREHOUSES,
     "Rule": "Slow seller: one warehouse only"},
    {"Product": "Kids' Socks",    "Velocity": "New",    "Storage": "Folded",
     "Demand": None, "OrderQty": 600,    "Allowed": WAREHOUSES,
     "Rule": "No sales history: split evenly"},
]


def products_table():
    rows = []
    for p in PRODUCTS:
        d = p["Demand"] or [None] * len(ZONES)
        row = {"Product": p["Product"], "Velocity": p["Velocity"], "Storage": p["Storage"]}
        row.update({z: v for z, v in zip(ZONES, d)})
        row["Total"] = sum(p["Demand"]) if p["Demand"] else p.get("OrderQty")
        row["Rule"] = p["Rule"] or "Optimizer decides"
        rows.append(row)
    return pd.DataFrame(rows)


def pre_run_checks(shares, capacity, products=PRODUCTS):
    """Return list of (level, message). level: ok / warn / error."""
    checks = []
    mn, mx = shares["Min %"], shares["Max %"]
    bad = shares.loc[mn > mx, "Warehouse"].tolist()
    if bad:
        checks.append(("error", f"Min is higher than max for: {', '.join(bad)}"))
    else:
        checks.append(("ok", "Each warehouse’s min is at or below its max"))
    if mx.sum() < 100:
        checks.append(("error", f"Max shares add up to {mx.sum():.0f}%, need at least 100%"))
    else:
        checks.append(("ok", f"Max shares add up to {mx.sum():.0f}% (100% or more)"))
    if mn.sum() > 100:
        checks.append(("error", f"Min shares add up to {mn.sum():.0f}%, must be 100% or less"))
    else:
        checks.append(("ok", f"Min shares add up to {mn.sum():.0f}% (100% or less)"))

    for k in STORAGE:
        need = sum((sum(p["Demand"]) if p["Demand"] else p.get("OrderQty", 0))
                   for p in products if p["Storage"] == k)
        have = capacity[k].sum()
        if need > have:
            checks.append(("warn", f"{k}: demand {need:,} is more than space {have:,}. "
                                   "Extra units will be placed with a penalty."))
        else:
            checks.append(("ok", f"{k}: demand {need:,} fits in space {have:,}"))
    return checks


@dataclass
class RunResult:
    status: str
    allocations: pd.DataFrame
    summary: pd.DataFrame
    overflow: pd.DataFrame
    warnings: list = field(default_factory=list)
    total_cost: float = 0.0
    units: int = 0


def run_allocation(shares, capacity, costs=DEFAULT_COSTS, penalty=25.0, products=PRODUCTS):
    cap = capacity.set_index("Warehouse")[STORAGE].astype(float).copy()
    shr = shares.set_index("Warehouse")
    rows, warnings = [], []

    # ---- 1. Rule-based placements ------------------------------------------
    lp_products = []
    for p in products:
        k = p["Storage"]
        if p["Velocity"] == "Low":
            demand = np.array(p["Demand"], dtype=float)
            total = demand.sum()
            lane_cost = {w: float(costs.loc[w].values @ demand) for w in p["Allowed"]}
            fits = [w for w in p["Allowed"] if cap.loc[w, k] >= total]
            pool = fits or p["Allowed"]
            best = min(pool, key=lambda w: lane_cost[w])
            for z, d in zip(ZONES, demand):
                rows.append([best, z, p["Product"], k, d, costs.loc[best, z], "Rule: slow seller"])
            cap.loc[best, k] -= total
            warnings.append(("info", f"{p['Product']} (slow seller) placed in {best}: "
                                     f"lowest shipping cost among warehouses with space."))
        elif p["Velocity"] == "New":
            allowed = [w for w in p["Allowed"] if cap.loc[w, k] > 0] or p["Allowed"]
            each = p["OrderQty"] / len(allowed)
            for w in allowed:
                rows.append([w, "Not known yet", p["Product"], k, each, 0.0, "Rule: even split"])
                cap.loc[w, k] -= each
            warnings.append(("info", f"{p['Product']} has no sales history: "
                                     f"{p['OrderQty']:,} units split evenly ({each:,.0f} each)."))
        else:
            lp_products.append(p)

    # ---- 2. Build the LP -----------------------------------------------------
    I, J, P, K = len(WAREHOUSES), len(ZONES), len(lp_products), len(STORAGE)
    nx = I * J * P
    no = I * K

    def xi(i, j, p):
        return (i * J + j) * P + p

    def oi(i, k):
        return nx + i * K + k

    c = np.zeros(nx + no)
    for i, w in enumerate(WAREHOUSES):
        for j, z in enumerate(ZONES):
            for p in range(P):
                c[xi(i, j, p)] = costs.loc[w, z]
    for i in range(I):
        for k in range(K):
            c[oi(i, k)] = penalty
    bounds = [(0, None)] * (nx + no)
    for i, w in enumerate(WAREHOUSES):
        for p, prod in enumerate(lp_products):
            if w not in prod["Allowed"]:          # exceptions + medium-velocity limits
                for j in range(J):
                    bounds[xi(i, j, p)] = (0, 0)

    A, b = [], []
    # demand: sum_i x >= d
    for j in range(J):
        for p, prod in enumerate(lp_products):
            row = np.zeros(nx + no)
            for i in range(I):
                row[xi(i, j, p)] = -1
            A.append(row); b.append(-prod["Demand"][j])
    # share: min_i <= sum x_i <= max_i  (share of optimizer units, as in the original)
    D = sum(sum(p["Demand"]) for p in lp_products)
    for i, w in enumerate(WAREHOUSES):
        row = np.zeros(nx + no)
        for j in range(J):
            for p in range(P):
                row[xi(i, j, p)] = 1
        A.append(row.copy()); b.append(shr.loc[w, "Max %"] / 100 * D)
        min_units = min(shr.loc[w, "Min %"] / 100 * D, 0.95 * max(cap.loc[w].sum(), 0))
        A.append(-row); b.append(-min_units)
    # space: sum x(storage k) - overflow <= cap
    for i, w in enumerate(WAREHOUSES):
        for k, kname in enumerate(STORAGE):
            row = np.zeros(nx + no)
            for j in range(J):
                for p, prod in enumerate(lp_products):
                    if prod["Storage"] == kname:
                        row[xi(i, j, p)] = 1
            row[oi(i, k)] = -1
            A.append(row); b.append(max(cap.loc[w, kname], 0))

    res = linprog(c, A_ub=np.array(A), b_ub=np.array(b), bounds=bounds, method="highs")
    if res.status != 0:
        return RunResult(status="No plan found: " + res.message,
                         allocations=pd.DataFrame(), summary=pd.DataFrame(),
                         overflow=pd.DataFrame(), warnings=[("high", res.message)])

    x = res.x
    for i, w in enumerate(WAREHOUSES):
        for j, z in enumerate(ZONES):
            for p, prod in enumerate(lp_products):
                v = x[xi(i, j, p)]
                if v > 0.5:
                    rows.append([w, z, prod["Product"], prod["Storage"], v,
                                 costs.loc[w, z], "Optimizer"])

    alloc = pd.DataFrame(rows, columns=["Warehouse", "Zone", "Product", "Storage",
                                        "Units", "Cost per unit", "Method"])
    alloc["Units"] = alloc["Units"].round().astype(int)
    alloc["Shipping cost"] = alloc["Units"] * alloc["Cost per unit"]

    overflow = pd.DataFrame(
        [[w, k, round(x[oi(i, kk)])] for i, w in enumerate(WAREHOUSES)
         for kk, k in enumerate(STORAGE)],
        columns=["Warehouse", "Storage", "Overflow"])

    # ---- 3. Summary by warehouse x storage ----------------------------------
    used = alloc.groupby(["Warehouse", "Storage"])["Units"].sum()
    space = capacity.set_index("Warehouse")[STORAGE]
    total_units = int(alloc["Units"].sum())
    srows = []
    for w in WAREHOUSES:
        for k in STORAGE:
            u = int(used.get((w, k), 0))
            s = int(space.loc[w, k])
            srows.append([w, k, u, s, u / s if s else 0.0])
    summary = pd.DataFrame(srows, columns=["Warehouse", "Storage", "Units", "Space", "Space used"])

    # ---- 4. Warnings ----------------------------------------------------------
    for _, r in overflow[overflow["Overflow"] > 0].iterrows():
        warnings.insert(0, ("high", f"{r.Warehouse} · {r.Storage} is {r.Overflow:,} units over "
                                    f"its space (penalty ${penalty:,.0f} per unit)."))
    for _, r in summary.iterrows():
        if 0.9 <= r["Space used"] <= 1.0:
            warnings.insert(0, ("medium", f"{r.Warehouse} · {r.Storage} is "
                                          f"{r['Space used']:.0%} full."))
    lp_units = alloc[alloc["Method"] == "Optimizer"].groupby("Warehouse")["Units"].sum()
    for w in WAREHOUSES:
        sh = lp_units.get(w, 0) / D * 100
        if sh >= shr.loc[w, "Max %"] - 0.5:
            warnings.insert(0, ("medium", f"{w} reached its maximum share "
                                          f"({shr.loc[w, 'Max %']:.0f}%). Some demand was sent "
                                          f"from a more expensive warehouse."))
        elif sh <= shr.loc[w, "Min %"] + 0.5:
            warnings.insert(0, ("medium", f"{w} is at its minimum share "
                                          f"({shr.loc[w, 'Min %']:.0f}%). It received extra units "
                                          f"to meet the floor."))

    order = {"high": 0, "medium": 1, "info": 2}
    warnings.sort(key=lambda t: order.get(t[0], 3))

    return RunResult(
        status="Optimal plan found",
        allocations=alloc,
        summary=summary,
        overflow=overflow,
        warnings=warnings,
        total_cost=float(alloc["Shipping cost"].sum()),
        units=total_units,
    )
