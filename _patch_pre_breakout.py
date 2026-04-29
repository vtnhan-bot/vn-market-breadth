"""One-off patcher: compute pre-breakout signals and inject the panel into market_breadth.html.

Idempotent: re-running replaces the existing panel.
Placement: right before the RS Heatmap panel.
"""
import re
import sys
from pathlib import Path

import pandas as pd

import pre_breakout

HERE = Path(__file__).parent
HTML_PATH = HERE / "market_breadth.html"

PANEL_MARKER = "<!-- PRE-BREAKOUT PANEL START -->"
PANEL_END    = "<!-- PRE-BREAKOUT PANEL END -->"


def _row_pct_below(value: float) -> str:
    return f"{value:+.2f}%"


def _table_layer_a(rows: list[dict], title: str, empty_msg: str) -> str:
    if not rows:
        return f"""
        <div class="pb-table-wrap">
          <div class="pb-subhead">{title}</div>
          <div class="pb-empty">{empty_msg}</div>
        </div>"""
    body = "".join(
        f"<tr><td class='pb-tkr'>{r['ticker']}</td>"
        f"<td>{r['close']:,.2f}</td>"
        f"<td class='pb-num'>{_row_pct_below(r['pct_below_52w_high'])}</td>"
        f"<td class='pb-num'>{_row_pct_below(r['rs_vs_peak_pct'])}</td></tr>"
        for r in rows
    )
    return f"""
        <div class="pb-table-wrap">
          <div class="pb-subhead">{title}</div>
          <table class="pb-table">
            <thead><tr><th>Ticker</th><th>Close</th><th>Δ vs 52w high</th><th>Δ RS vs peak</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
        </div>"""


def _table_layer_b(rows: list[dict], title: str, empty_msg: str) -> str:
    if not rows:
        return f"""
        <div class="pb-table-wrap">
          <div class="pb-subhead">{title}</div>
          <div class="pb-empty">{empty_msg}</div>
        </div>"""
    body = "".join(
        f"<tr><td class='pb-tkr'>{r['ticker']}</td>"
        f"<td>{r['close']:,.2f}</td>"
        f"<td class='pb-num'><b>{r['rs_ratio']:.3f}</b></td>"
        f"<td class='pb-num'>{r['stock_ret_6mo_pct']:+.1f}%</td>"
        f"<td class='pb-num'>{r['bb_width_pct']:.2f}%</td>"
        f"<td class='pb-num'>{r['bb_width_percentile']:.0f}%</td></tr>"
        for r in rows
    )
    return f"""
        <div class="pb-table-wrap">
          <div class="pb-subhead">{title}</div>
          <table class="pb-table">
            <thead><tr><th>Ticker</th><th>Close</th><th>RS_Ratio</th><th>Stock 6m</th><th>BB Width</th><th>BB %ile</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
        </div>"""


def _table_both(both: list[dict]) -> str:
    if not both:
        return ""
    body = "".join(
        f"<tr><td class='pb-tkr'>{x['ticker']}</td>"
        f"<td>{x['a']['close']:,.2f}</td>"
        f"<td class='pb-num'>{_row_pct_below(x['a']['pct_below_52w_high'])}</td>"
        f"<td class='pb-num'><b>{x['b']['rs_ratio']:.3f}</b></td>"
        f"<td class='pb-num'>{x['b']['bb_width_percentile']:.0f}%</td></tr>"
        for x in both
    )
    return f"""
        <div class="pb-both">
          <div class="pb-subhead pb-both-head">⭐ Cả 2 tín hiệu cùng kích hoạt — cấu hình mạnh nhất</div>
          <table class="pb-table">
            <thead><tr><th>Ticker</th><th>Close</th><th>Δ vs 52w high</th><th>RS_Ratio</th><th>BB %ile</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
        </div>"""


def build_panel(result: pre_breakout.PreBreakoutResult) -> str:
    meta = result.meta
    coverage = (
        f"Phân tích {meta['analyzed_count']}/{meta['universe_count']} mã RS "
        f"(thiếu OHLC cho {meta['missing_count']} mã)"
    )
    p = meta["params"]
    methodology = (
        f"<b>Layer A</b>: RS Line ≥ {p['rs_high_tolerance']*100:.0f}% đỉnh "
        f"{p['window_52w']} phiên & giá &lt; {p['price_base_max']*100:.0f}% đỉnh giá. "
        f"<b>Layer B</b>: RS_Ratio = (1+stock_{p['return_lookback_days']}d) / (1+vni_{p['return_lookback_days']}d) > "
        f"{p['rs_ratio_threshold']:.2f} & BB({p['bb_period']},{p['bb_k']:.0f}σ) width "
        f"trong {p['squeeze_percentile']:.0f}% thấp nhất {p['return_lookback_days']} phiên gần nhất."
    )

    return f"""{PANEL_MARKER}
  <div class="panel pb-panel">
    <h2>🚀 Cổ phiếu sắp bùng nổ <span class="tag">Pre-breakout</span></h2>
    <div class="pb-meta">{coverage}</div>

    {_table_both(result.both)}

    <div class="pb-grid">
      <div>
        <div class="pb-layer-head">📈 Layer A — RS Line Divergence</div>
        <div class="pb-desc">RS Line lập đỉnh 52 tuần trong khi giá vẫn đang trong nền (chưa break ra).</div>
        {_table_layer_a(result.layer_a, '🔥 Đã kích hoạt', 'Không có mã nào đáp ứng tiêu chí nghiêm ngặt hôm nay.')}
        {_table_layer_a(result.layer_a_watch, '👀 Theo dõi (top 10 trong nền, RS gần đỉnh)', '—')}
      </div>
      <div>
        <div class="pb-layer-head">🎯 Layer B — RS_Ratio + BB Squeeze</div>
        <div class="pb-desc">Vượt VNINDEX trên 20% trong 6 tháng đồng thời Bollinger Band thắt chặt → sắp bùng nổ.</div>
        {_table_layer_b(result.layer_b, '🔥 Đã kích hoạt', 'Không có mã nào đáp ứng tiêu chí nghiêm ngặt hôm nay.')}
        {_table_layer_b(result.layer_b_watch, '👀 Theo dõi (top 10 RS_Ratio cao + BB siết)', '—')}
      </div>
    </div>

    <div class="pb-method">{methodology}</div>
  </div>
  {PANEL_END}
"""


PANEL_CSS = """
    /* Pre-breakout panel */
    .pb-panel { background: #fffaf3; border: 1px solid #ead8c0; }
    .pb-panel h2 { color: #b03a2e; }
    .pb-meta { font-size: 0.82rem; color: #7a6750; margin-bottom: 12px; }
    .pb-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 14px; }
    @media (max-width: 800px) { .pb-grid { grid-template-columns: 1fr; } }
    .pb-layer-head { font-size: 1rem; font-weight: 700; color: #b03a2e; margin-bottom: 4px; }
    .pb-desc { font-size: 0.82rem; color: #6d4f00; margin-bottom: 10px; }
    .pb-table-wrap { margin-bottom: 14px; }
    .pb-subhead { font-size: 0.85rem; font-weight: 700; color: #444; margin: 10px 0 4px; }
    .pb-empty { font-size: 0.85rem; color: #999; padding: 6px 0; font-style: italic; }
    .pb-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; background: #fff; border: 1px solid #ead8c0; border-radius: 6px; overflow: hidden; }
    .pb-table th { background: #f5e6cf; padding: 6px 8px; text-align: center; font-weight: 600; color: #4a3000; }
    .pb-table td { padding: 5px 8px; border-bottom: 1px solid #f5ecd9; text-align: center; }
    .pb-table tr:last-child td { border-bottom: none; }
    .pb-table tr:hover td { background: #fff8eb; }
    .pb-tkr { font-weight: 700; color: #1f2937; text-align: left !important; }
    .pb-num { font-variant-numeric: tabular-nums; }
    .pb-both { background: #fff5e0; border: 2px solid #f0c36d; border-radius: 6px; padding: 10px 12px; margin-bottom: 14px; }
    .pb-both-head { color: #8a5a00; }
    .pb-method { font-size: 0.78rem; color: #6d4f00; margin-top: 14px; padding: 10px; background: #fdf6e9; border-radius: 4px; line-height: 1.6; }
"""


def main() -> None:
    candidates = sorted(HERE.glob("data/*/combined_dataset.csv"))
    if not candidates:
        print("ERROR: No data/*/combined_dataset.csv found.", file=sys.stderr)
        sys.exit(1)
    latest = candidates[-1]
    print(f"Computing pre-breakout from {latest.name} ({latest.parent.name})...")
    result = pre_breakout.compute(latest, HERE / "rs_universe.csv")
    print(
        f"  layer_a={len(result.layer_a)}  watch_a={len(result.layer_a_watch)}  "
        f"layer_b={len(result.layer_b)}  watch_b={len(result.layer_b_watch)}  "
        f"both={len(result.both)}  analyzed={result.meta['analyzed_count']}/{result.meta['universe_count']}"
    )

    panel_html = build_panel(result)

    html = HTML_PATH.read_text(encoding="utf-8")

    # 1) Inject CSS once into <style> block
    if "/* Pre-breakout panel */" not in html:
        html = html.replace("</style>", PANEL_CSS + "\n  </style>", 1)

    # 2) Replace existing panel if present, else insert before RS heatmap
    if PANEL_MARKER in html:
        html = re.sub(
            re.escape(PANEL_MARKER) + r".*?" + re.escape(PANEL_END),
            panel_html.strip("\n"),
            html,
            count=1,
            flags=re.DOTALL,
        )
    else:
        rs_anchor = '<div class="panel">\n    <h2>Relative Strength Heatmap'
        if rs_anchor not in html:
            print("ERROR: RS Heatmap anchor not found in HTML; cannot place panel.", file=sys.stderr)
            sys.exit(2)
        html = html.replace(rs_anchor, panel_html + "  " + rs_anchor, 1)

    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"Patched {HTML_PATH}")


if __name__ == "__main__":
    main()
