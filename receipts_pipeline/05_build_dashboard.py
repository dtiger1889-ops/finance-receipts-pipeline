"""
Phase E: build dashboard.html

Reads from receipts.db and produces a single self-contained HTML file with
interactive Plotly charts. Open in any browser, no server.

Data model: Monarch is the spending truth. Receipts and Amazon orders are
qualitative behavioral context only.

Coverage caveat: Monarch goes back to 2022 for some accounts, but the Chase
credit card data (largest spend account) only starts 2024-04. So spending
charts default to a 2024-04-01 window. Pre-2024-04 is shown only in
behavioral context panels.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PIPELINE_DIR = Path(__file__).resolve().parent
DB_PATH = PIPELINE_DIR / "receipts.db"
OUTPUT_HTML = PIPELINE_DIR / "dashboard.html"

# Categories that represent inter-account moves or income, not "spending"
NON_SPENDING_CATEGORIES = {
    "Credit Card Payment", "Transfer", "Paychecks", "Other Income", "Interest",
}
SPENDING_WINDOW_START = "2024-04-01"


def load_dataframes(conn):
    monarch = pd.read_sql_query("""
        SELECT id, account_name, txn_date, merchant, merchant_norm,
               category, original_statement, amount_cents
        FROM monarch_transactions
    """, conn, parse_dates=["txn_date"])

    receipts = pd.read_sql_query("""
        SELECT message_id_hash, mbox_file, label_path, sent_date_local,
               sender_domain, subject, amount_cents, merchant_norm,
               extraction_method, extraction_confidence
        FROM receipts
    """, conn)
    # sent_date_local has mixed timezones (EDT/EST). Parse with utc=True then convert.
    receipts["sent_date_local"] = pd.to_datetime(receipts["sent_date_local"], utc=True, errors="coerce").dt.tz_convert("America/New_York")

    amazon = pd.read_sql_query("""
        SELECT order_id, order_date, product_name, total_amount_cents, source
        FROM amazon_orders
    """, conn, parse_dates=["order_date"])

    matches = pd.read_sql_query("""
        SELECT m.monarch_id, m.receipt_id, m.match_type, m.merchant_similarity,
               r.subject AS receipt_subject, r.body_path
        FROM matches m JOIN receipts r ON r.message_id_hash = m.receipt_id
    """, conn)

    return monarch, receipts, amazon, matches


def spending_df(monarch: pd.DataFrame, since: str = SPENDING_WINDOW_START) -> pd.DataFrame:
    df = monarch[monarch["amount_cents"] < 0].copy()
    df = df[~df["category"].isin(NON_SPENDING_CATEGORIES)]
    df["spend"] = -df["amount_cents"] / 100.0
    df["txn_date"] = pd.to_datetime(df["txn_date"])
    df = df[df["txn_date"] >= since]
    return df


def kpi_cards_html(spending: pd.DataFrame) -> str:
    today = pd.Timestamp(date.today())
    s30 = spending[spending["txn_date"] >= today - pd.Timedelta(days=30)]["spend"].sum()
    s90 = spending[spending["txn_date"] >= today - pd.Timedelta(days=90)]["spend"].sum()
    s365 = spending[spending["txn_date"] >= today - pd.Timedelta(days=365)]["spend"].sum()
    s30_prev = spending[(spending["txn_date"] >= today - pd.Timedelta(days=60)) & (spending["txn_date"] < today - pd.Timedelta(days=30))]["spend"].sum()
    s90_prev = spending[(spending["txn_date"] >= today - pd.Timedelta(days=180)) & (spending["txn_date"] < today - pd.Timedelta(days=90))]["spend"].sum()

    def delta(curr, prev):
        if prev == 0:
            return ""
        pct = (curr - prev) / prev * 100
        sign = "+" if pct >= 0 else ""
        color = "#c44" if pct >= 0 else "#3a3"
        return f'<span style="color:{color};font-size:0.85em">{sign}{pct:.0f}% vs prior</span>'

    return f"""
    <div class="kpi-row">
      <div class="kpi"><div class="kpi-label">Last 30 days</div><div class="kpi-value">${s30:,.0f}</div><div>{delta(s30, s30_prev)}</div></div>
      <div class="kpi"><div class="kpi-label">Last 90 days</div><div class="kpi-value">${s90:,.0f}</div><div>{delta(s90, s90_prev)}</div></div>
      <div class="kpi"><div class="kpi-label">Last 365 days</div><div class="kpi-value">${s365:,.0f}</div></div>
    </div>
    """


def fig_monthly_categories(spending: pd.DataFrame) -> go.Figure:
    df = spending.copy()
    df["month"] = df["txn_date"].dt.to_period("M").dt.to_timestamp()
    pivot = df.groupby(["month", "category"])["spend"].sum().reset_index()
    fig = px.bar(pivot, x="month", y="spend", color="category",
                 title="Monthly spending by category",
                 labels={"spend": "$", "month": "Month"})
    fig.update_layout(barmode="stack", height=450, hovermode="x unified")
    return fig


def fig_top_merchants(spending: pd.DataFrame, n: int = 25) -> go.Figure:
    top = spending.groupby("merchant").agg(total=("spend", "sum"), count=("spend", "size")).sort_values("total", ascending=False).head(n)
    fig = go.Figure(go.Bar(
        x=top["total"], y=top.index,
        orientation="h",
        text=[f"${v:,.0f} ({c}x)" for v, c in zip(top["total"], top["count"])],
        textposition="outside",
        marker_color="#4a86c7",
    ))
    fig.update_layout(
        title=f"Top {n} merchants by total spend (Apr 2024+)",
        yaxis={"autorange": "reversed"},
        height=600,
        margin=dict(l=180, r=140),
    )
    return fig


def fig_calendar_heatmap(spending: pd.DataFrame) -> go.Figure:
    """GitHub-style heatmap: weeks across, weekdays down, $/day as color."""
    df = spending.copy()
    daily = df.groupby(df["txn_date"].dt.date)["spend"].sum().reset_index()
    daily["txn_date"] = pd.to_datetime(daily["txn_date"])

    # Last 365 days
    today = pd.Timestamp(date.today())
    start = today - pd.Timedelta(days=365)
    full_range = pd.DataFrame({"txn_date": pd.date_range(start, today, freq="D")})
    daily = full_range.merge(daily, on="txn_date", how="left").fillna({"spend": 0})

    daily["weekday"] = daily["txn_date"].dt.dayofweek  # 0=Mon
    daily["week_start"] = daily["txn_date"] - pd.to_timedelta(daily["weekday"], unit="D")
    pivot = daily.pivot_table(index="weekday", columns="week_start", values="spend", aggfunc="sum", fill_value=0)

    fig = go.Figure(go.Heatmap(
        z=pivot.values,
        x=pivot.columns,
        y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        colorscale="YlOrRd",
        hovertemplate="%{x|%a %b %d}<br>$%{z:.2f}<extra></extra>",
        colorbar=dict(title="$/day"),
    ))
    fig.update_layout(title="Daily spending heatmap (last 365 days)", height=300)
    return fig


def fig_subscriptions(spending: pd.DataFrame) -> go.Figure:
    """Auto-detect recurring same-merchant same-amount monthly charges."""
    df = spending.copy()
    df["month"] = df["txn_date"].dt.to_period("M")
    grouped = df.groupby(["merchant", "amount_cents"]).agg(
        n_months=("month", "nunique"),
        n=("amount_cents", "size"),
        first=("txn_date", "min"),
        last=("txn_date", "max"),
    ).reset_index()
    candidates = grouped[grouped["n_months"] >= 3].copy()
    candidates["amount"] = -candidates["amount_cents"] / 100.0
    candidates["annualized"] = candidates["amount"] * 12
    candidates = candidates.sort_values("annualized", ascending=False).head(30)

    fig = go.Figure(go.Bar(
        x=candidates["annualized"], y=candidates["merchant"] + " ($" + candidates["amount"].round(2).astype(str) + ")",
        orientation="h",
        text=[f"${v:,.0f}/yr" for v in candidates["annualized"]],
        textposition="outside",
        marker_color="#6a4c93",
        hovertemplate="%{y}<br>Annualized: $%{x:,.2f}<br>Months seen: %{customdata[0]}<br>First: %{customdata[1]}<br>Last: %{customdata[2]}<extra></extra>",
        customdata=list(zip(candidates["n_months"], candidates["first"].dt.strftime("%Y-%m-%d"), candidates["last"].dt.strftime("%Y-%m-%d"))),
    ))
    fig.update_layout(
        title="Auto-detected recurring charges (same merchant + same amount, ≥3 months)",
        yaxis={"autorange": "reversed"},
        height=600,
        margin=dict(l=260, r=140),
    )
    return fig


def fig_yoy(spending: pd.DataFrame) -> go.Figure:
    df = spending.copy()
    df["year"] = df["txn_date"].dt.year
    df["month"] = df["txn_date"].dt.month
    pivot = df.groupby(["year", "month"])["spend"].sum().reset_index()

    fig = go.Figure()
    for year in sorted(pivot["year"].unique()):
        sub = pivot[pivot["year"] == year]
        fig.add_trace(go.Scatter(x=sub["month"], y=sub["spend"], mode="lines+markers", name=str(year)))
    fig.update_layout(
        title="Year-over-year monthly spending",
        xaxis=dict(tickmode="array", tickvals=list(range(1, 13)),
                   ticktext=["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]),
        yaxis_title="$",
        height=400,
        hovermode="x unified",
    )
    return fig


def fig_rideshare_patterns(receipts: pd.DataFrame) -> go.Figure:
    df = receipts[receipts["merchant_norm"].isin(["uber", "lyft", "capital bikeshare"])].copy()
    df = df[df["amount_cents"].notna()]
    df["amount"] = df["amount_cents"] / 100.0
    df["month"] = df["sent_date_local"].dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()
    monthly = df.groupby(["month", "merchant_norm"]).agg(rides=("amount", "size"), spend=("amount", "sum")).reset_index()

    fig = make_subplots(rows=1, cols=2, subplot_titles=("Rides per month", "Spend per month"))
    for merchant in ["uber", "lyft", "capital bikeshare"]:
        sub = monthly[monthly["merchant_norm"] == merchant]
        fig.add_trace(go.Scatter(x=sub["month"], y=sub["rides"], mode="lines+markers", name=merchant, legendgroup=merchant), row=1, col=1)
        fig.add_trace(go.Scatter(x=sub["month"], y=sub["spend"], mode="lines+markers", name=merchant, legendgroup=merchant, showlegend=False), row=1, col=2)
    fig.update_layout(title="Rideshare behavior — Uber / Lyft / Capital Bikeshare (full receipt history)", height=400)
    return fig


def fig_amazon_mix(amazon: pd.DataFrame) -> go.Figure:
    df = amazon[amazon["source"] == "order_history_csv"].copy()
    df = df[df["order_date"].notna() & df["total_amount_cents"].notna()]
    df["spend"] = df["total_amount_cents"] / 100.0
    df["year"] = df["order_date"].dt.year
    yearly = df.groupby("year").agg(orders=("order_id", "nunique"), spend=("spend", "sum"), avg_order=("spend", "mean")).reset_index()

    fig = make_subplots(rows=1, cols=3, subplot_titles=("Orders/year", "Total spend/year", "Avg order value"))
    fig.add_trace(go.Bar(x=yearly["year"], y=yearly["orders"], marker_color="#ff9900"), row=1, col=1)
    fig.add_trace(go.Bar(x=yearly["year"], y=yearly["spend"], marker_color="#ff9900"), row=1, col=2)
    fig.add_trace(go.Bar(x=yearly["year"], y=yearly["avg_order"], marker_color="#ff9900"), row=1, col=3)
    fig.update_layout(title="Amazon shopping behavior (Order History.csv only — does not double-count mbox)", height=350, showlegend=False)
    return fig


def recent_transactions_html(monarch: pd.DataFrame, matches: pd.DataFrame, n: int = 50) -> str:
    df = monarch.copy()
    df = df[df["amount_cents"] < 0]
    df = df[~df["category"].isin(NON_SPENDING_CATEGORIES)]
    df = df.sort_values("txn_date", ascending=False).head(n)

    matches_idx = matches.set_index("monarch_id")
    rows = []
    for _, row in df.iterrows():
        mid = row["id"]
        receipt_html = ""
        if mid in matches_idx.index:
            m = matches_idx.loc[mid]
            if isinstance(m, pd.DataFrame):
                m = m.iloc[0]
            sub = (m["receipt_subject"] or "")[:80]
            receipt_html = f'<span style="color:#3a3" title="match similarity: {m["merchant_similarity"]:.0f}">📧 {sub}</span>'
        rows.append(
            f"<tr>"
            f"<td>{row['txn_date'].strftime('%Y-%m-%d')}</td>"
            f"<td>{row['merchant'] or ''}</td>"
            f"<td>{row['category'] or ''}</td>"
            f"<td style='text-align:right'>${-row['amount_cents']/100:.2f}</td>"
            f"<td>{receipt_html}</td>"
            f"</tr>"
        )
    return f"""
    <table class="recent">
      <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Amount</th><th>Receipt</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def coverage_banner(monarch: pd.DataFrame, matches: pd.DataFrame, receipts_total: int) -> str:
    n_monarch = len(monarch)
    n_matched = len(matches)
    debits = (monarch["amount_cents"] < 0).sum()
    return f"""
    <div class="banner">
      <strong>Data coverage</strong>:
      Monarch transactions <b>{n_monarch:,}</b> ({(monarch['txn_date'].min()).strftime('%Y-%m-%d')} → {(monarch['txn_date'].max()).strftime('%Y-%m-%d')}).
      Email receipts: <b>{receipts_total:,}</b>.
      Receipt-to-Monarch matches: <b>{n_matched:,}</b> of {debits:,} debits ({100*n_matched/max(debits,1):.0f}%).
      <br><b>Caveat:</b> spending charts default to <b>2024-04-01+</b> because Chase credit card data starts then.
      Pre-2024-04 spending is incomplete and would be misleading on the totals; behavioral context panels (rideshare, Amazon) use full history since they're not Monarch-driven.
    </div>
    """


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Financial dashboard</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; max-width: 1400px; color: #222; }}
  h1 {{ font-weight: 600; }}
  h2 {{ border-bottom: 2px solid #ccc; padding-bottom: 4px; margin-top: 36px; }}
  .banner {{ background: #fffbe6; border: 1px solid #f6e0a4; padding: 12px 16px; border-radius: 6px; margin: 12px 0 24px; line-height: 1.4; }}
  .kpi-row {{ display: flex; gap: 16px; margin: 24px 0; }}
  .kpi {{ flex: 1; background: #f5f7fa; padding: 16px; border-radius: 8px; }}
  .kpi-label {{ font-size: 0.9em; color: #666; }}
  .kpi-value {{ font-size: 2em; font-weight: 600; color: #2c4a7a; }}
  table.recent {{ border-collapse: collapse; width: 100%; font-size: 0.9em; }}
  table.recent th, table.recent td {{ padding: 6px 10px; border-bottom: 1px solid #eee; text-align: left; }}
  table.recent th {{ background: #f5f7fa; }}
  .footnote {{ color: #888; font-size: 0.85em; margin: 36px 0 0; }}
</style>
</head>
<body>
  <h1>Financial dashboard</h1>
  <div style="color:#888;font-size:0.9em">Generated {generated_at} from receipts.db</div>
  {coverage_banner}

  <h2>Spending overview (Monarch — source of truth)</h2>
  {kpi_cards}
  {fig_monthly_categories}
  {fig_top_merchants}
  {fig_calendar_heatmap}

  <h2>Subscriptions / recurring charges</h2>
  <p style="color:#666">Heuristic: same merchant + same amount appearing in 3+ different months. Hover for first/last seen.</p>
  {fig_subscriptions}

  <h2>Year-over-year</h2>
  {fig_yoy}

  <h2>Behavioral context (receipts/Amazon — NOT spending totals)</h2>
  <p style="color:#666">These panels show <em>behavior</em> (frequency, mix, patterns) sourced from email receipts and Amazon Order History. They aren't spending totals — those come from Monarch above. Full receipt history is shown (back to ~2008 for some senders) since this isn't constrained by the Monarch coverage window.</p>
  {fig_rideshare}
  {fig_amazon}

  <h2>Recent transactions (with matched receipts)</h2>
  <p style="color:#666">Last 50 spending transactions from Monarch. 📧 indicates a matched receipt providing qualitative context.</p>
  {recent_transactions}

  <div class="footnote">
    <p>Source files (read-only — never modified by this pipeline):</p>
    <ul>
      <li><code>Monarch Exports/</code> — 6 account CSVs</li>
      <li><code>Prime Analysis/Your Orders/Your Amazon Orders/Order History.csv</code></li>
      <li><code>2026-04-25 Gmail Financial Label Export/Mail/</code> — 19 mbox files</li>
    </ul>
    <p>Refresh: drop new exports, then run <code>python 03_load_reference.py &amp;&amp; python 04_build_matches.py &amp;&amp; python 05_build_dashboard.py</code>.</p>
  </div>
</body>
</html>
"""


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    monarch, receipts, amazon, matches = load_dataframes(conn)
    receipts_total = len(receipts)
    spending = spending_df(monarch)

    print(f"Building dashboard with {len(spending)} spending rows since {SPENDING_WINDOW_START}...")

    figures = {
        "fig_monthly_categories": fig_monthly_categories(spending),
        "fig_top_merchants": fig_top_merchants(spending),
        "fig_calendar_heatmap": fig_calendar_heatmap(spending),
        "fig_subscriptions": fig_subscriptions(spending),
        "fig_yoy": fig_yoy(spending),
        "fig_rideshare": fig_rideshare_patterns(receipts),
        "fig_amazon": fig_amazon_mix(amazon),
    }
    fig_html = {}
    first = True
    for name, fig in figures.items():
        # Inline plotly.js only on the first figure; reuse for the rest.
        fig_html[name] = fig.to_html(full_html=False, include_plotlyjs="inline" if first else False)
        first = False

    html = HTML_TEMPLATE.format(
        generated_at=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M %Z"),
        coverage_banner=coverage_banner(monarch, matches, receipts_total),
        kpi_cards=kpi_cards_html(spending),
        recent_transactions=recent_transactions_html(monarch, matches),
        **fig_html,
    )

    OUTPUT_HTML.write_text(html, encoding="utf-8")
    size_kb = OUTPUT_HTML.stat().st_size / 1024
    print(f"Wrote {OUTPUT_HTML} ({size_kb:.0f} KB)")
    print(f"Open in browser: file:///{OUTPUT_HTML.as_posix()}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
