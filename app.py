import altair as alt
import pandas as pd
import streamlit as st

import engine as e

st.set_page_config(page_title="Allocation Planner", page_icon="📦", layout="wide")

BLUE = "#2a78d6"


def money(v):
    return f"${v:,.0f}"


# ---------------------------------------------------------------- header
st.title("Allocation Planner")
st.caption("Decide how many units of each product each warehouse should stock, "
           "at the lowest shipping cost. Demo with sample data.")

if "result" not in st.session_state:
    st.session_state.result = None

tab_setup, tab_plan, tab_warn, tab_about = st.tabs(
    ["1 · Set up & run", "2 · See the plan", "3 · Check warnings", "About"])

# ---------------------------------------------------------------- 1. setup
with tab_setup:
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("New monthly run")
        month = st.selectbox("Month", ["November 2026", "December 2026", "January 2027"])

        st.markdown("**Warehouse share limits** (share of optimizer units)")
        shares = st.data_editor(
            e.DEFAULT_SHARES, hide_index=True, width="stretch", key="shares",
            disabled=["Warehouse"],
            column_config={
                "Min %": st.column_config.NumberColumn(min_value=0, max_value=100, step=5, format="%d%%"),
                "Max %": st.column_config.NumberColumn(min_value=0, max_value=100, step=5, format="%d%%"),
            })

        st.markdown("**Space available** (units)")
        capacity = st.data_editor(
            e.DEFAULT_CAPACITY, hide_index=True, width="stretch", key="capacity",
            disabled=["Warehouse"],
            column_config={
                "Folded": st.column_config.NumberColumn(min_value=0, step=500, format="localized"),
                "Hanging": st.column_config.NumberColumn(min_value=0, step=500, format="localized"),
            })

        penalty = st.number_input("Penalty for each unit over space ($)", min_value=1, value=25, step=5)

        with st.expander("Products and demand (sample data)"):
            st.dataframe(e.products_table(), hide_index=True, width="stretch",
                         column_config={z: st.column_config.NumberColumn(format="localized")
                                        for z in e.ZONES + ["Total"]})
            st.markdown("**Shipping cost per unit**")
            st.dataframe(e.DEFAULT_COSTS.style.format("${:.2f}"), width="stretch")

    with right:
        st.subheader("Checks before running")
        checks = e.pre_run_checks(shares, capacity)
        for level, msg in checks:
            if level == "ok":
                st.markdown(f":green[✓] {msg}")
            elif level == "warn":
                st.markdown(f":orange[!] {msg}")
            else:
                st.markdown(f":red[✕] {msg}")
        has_error = any(level == "error" for level, _ in checks)

        st.write("")
        if st.button("Run allocation", type="primary", width="stretch", disabled=has_error):
            with st.spinner("Placing products and solving…"):
                st.session_state.result = e.run_allocation(shares, capacity, penalty=float(penalty))
                st.session_state.month = month
            r = st.session_state.result
            if r.status.startswith("Optimal"):
                st.success(f"Plan ready for {month}. Open **2 · See the plan**.")
            else:
                st.error(r.status)
        if has_error:
            st.caption("Fix the ✕ items to enable the run.")

# ---------------------------------------------------------------- 2. plan
with tab_plan:
    r = st.session_state.result
    if r is None or r.allocations.empty:
        st.info("No plan yet. Go to **1 · Set up & run** and press **Run allocation**.")
    else:
        st.subheader(f"Plan for {st.session_state.get('month', '')}")
        st.success(f"✓ {r.status}")

        high = sum(1 for lvl, _ in r.warnings if lvl in ("high", "medium"))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total shipping cost", money(r.total_cost))
        m2.metric("Units placed", f"{r.units:,}")
        m3.metric("Average cost per unit", f"${r.total_cost / r.units:,.2f}")
        m4.metric("Things to check", f"{high}")

        col_t, col_c = st.columns([3, 2], gap="large")
        with col_t:
            st.markdown("**Space used by warehouse**")
            st.dataframe(
                r.summary, hide_index=True, width="stretch",
                column_config={
                    "Units": st.column_config.NumberColumn(format="localized"),
                    "Space": st.column_config.NumberColumn(format="localized"),
                    "Space used": st.column_config.ProgressColumn(
                        format="percent", min_value=0, max_value=1),
                })
        with col_c:
            st.markdown("**Shipping cost by warehouse**")
            by_w = (r.allocations.groupby("Warehouse", as_index=False)["Shipping cost"].sum())
            by_w["Label"] = by_w["Shipping cost"].map(money)
            base = alt.Chart(by_w).encode(
                x=alt.X("Warehouse:N", sort=e.WAREHOUSES, title=None,
                        axis=alt.Axis(labelAngle=0, domain=False, ticks=False)),
                y=alt.Y("Shipping cost:Q", title=None,
                        axis=alt.Axis(format="$,.0f", grid=True, gridOpacity=0.4,
                                      domain=False, ticks=False, tickCount=4)),
                tooltip=[alt.Tooltip("Warehouse:N"),
                         alt.Tooltip("Shipping cost:Q", format="$,.0f")],
            )
            bars = base.mark_bar(color=BLUE, cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                                 size=48)
            labels = base.mark_text(dy=-8, color="#52514e", fontSize=13).encode(text="Label:N")
            st.altair_chart((bars + labels).properties(height=260).configure_view(strokeWidth=0),
                            width="stretch")

        st.markdown("**Units by product and warehouse**")
        pivot = (r.allocations.pivot_table(index="Product", columns="Warehouse", values="Units",
                                           aggfunc="sum", fill_value=0)
                 .reindex(columns=e.WAREHOUSES, fill_value=0))
        pivot["Total"] = pivot.sum(axis=1)
        method = r.allocations.groupby("Product")["Method"].first()
        pivot.insert(0, "Placed by", method)
        st.dataframe(pivot, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="localized")
                                    for c in e.WAREHOUSES + ["Total"]})

        with st.expander("All shipping lanes"):
            st.dataframe(
                r.allocations.sort_values(["Warehouse", "Product", "Zone"]),
                hide_index=True, width="stretch",
                column_config={
                    "Units": st.column_config.NumberColumn(format="localized"),
                    "Cost per unit": st.column_config.NumberColumn(format="dollar"),
                    "Shipping cost": st.column_config.NumberColumn(format="dollar"),
                })

        b1, b2, _ = st.columns([1, 1, 3])
        b1.download_button("Export CSV", r.allocations.to_csv(index=False).encode(),
                           file_name="allocation_plan.csv", mime="text/csv",
                           width="stretch")
        if b2.button("Approve plan", type="primary", width="stretch"):
            st.toast("Plan approved ✓")

# ---------------------------------------------------------------- 3. warnings
with tab_warn:
    r = st.session_state.result
    if r is None:
        st.info("Run the allocation first to see warnings.")
    else:
        st.subheader(f"Things to check · {len(r.warnings)}")
        icon = {"high": ":red[**High**]", "medium": ":orange[**Check**]", "info": ":blue[**Info**]"}
        for level, msg in r.warnings:
            with st.container(border=True):
                st.markdown(f"{icon.get(level, '')} &nbsp; {msg}")
        if not r.warnings:
            st.success("Nothing to check. The plan fits every limit.")

# ---------------------------------------------------------------- about
with tab_about:
    st.subheader("How it works")
    st.markdown(
        """
1. **Rule-based products first.** Slow sellers go to one warehouse (the cheapest one with
   space). Products with no sales history are split evenly.
2. **Space is reduced** by what the rules used.
3. **The optimizer places everything else** at the lowest shipping cost, while:
   - meeting every zone's demand,
   - keeping each warehouse between its min and max share,
   - staying within space (going over is allowed only at a penalty),
   - following business rules (Fragrance not in West; Dresses and Outerwear limited to two warehouses).

**Who it's for:** Maria, an allocation planner who runs the plan monthly and needs a clear
summary and a short list of problems, without reading log files.
"""
    )
