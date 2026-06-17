#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Renderiza o e-mail semanal do Alocador (HTML inline p/ clientes de e-mail)."""

def _brl(n):
    return "—" if n is None else "R$ " + format(int(round(n)), ",d").replace(",", ".")

def render_email(out, cfg):
    d = out["decision"]; m = out["macro"]; b = out["banda"]
    cash = d["cashpct"] * 100; tgt = d["caixa_alvo_pct"] * 100
    score = out["composite"]
    scol = ("#6b7280" if score is None else "#b4322a" if score < 3.5 else "#c2541f"
            if score < 5.5 else "#b07d0a" if score < 6.5 else "#1a7a52" if score < 7.5
            else "#1d4ed8" if score < 9.5 else "#6d28d9")

    def sleeve_rows():
        out_html = ""
        for s in out["sleeves"]:
            if s["aloc"] <= 0 and s["gate"] != "fechado":
                continue
            tag = ("<span style='color:#b4322a'>gate fechado</span>" if s["gate"] == "fechado"
                   else "<b style='color:#1a7a52'>" + _brl(s["aloc"]) + "</b>")
            out_html += (f"<tr><td style='padding:5px 0'>{s['name']} "
                         f"<span style='color:#9a988f;font-size:11px'>({s['alvo']*100:.0f}% alvo · "
                         f"{s['atual']*100:.0f}% atual)</span></td>"
                         f"<td style='text-align:right'>{tag}</td></tr>")
        return out_html

    def groups_rows():
        r = ""
        for g, v in out["groups"].items():
            r += (f"<tr><td colspan='2' style='padding-top:10px;font-weight:bold'>{g} "
                  f"<span style='color:#9a988f;font-weight:normal'>· {v['weight']*100:.0f}%</span></td></tr>")
            for i in v["items"]:
                sc = i["score"]
                bg = ("#eef0ee" if sc is None else "#e8f5ee" if sc >= 7
                      else "#fbf3df" if sc >= 5 else "#f7e7e4")
                r += (f"<tr><td style='padding:3px 0;color:#555'>{i['key']} "
                      f"<span style='color:#9a988f;font-size:11px'>{i['read']}</span></td>"
                      f"<td style='text-align:right'><span style='background:{bg};"
                      f"padding:1px 8px;border-radius:8px;font-weight:bold'>"
                      f"{'—' if sc is None else f'{sc:.1f}'}</span></td></tr>")
        return r

    flags = "".join(
        f"<div style='background:{'#f7e7e4' if f['level']=='alto' else '#fbf3df' if f['level']=='medio' else '#e8f5ee'};"
        f"color:{'#b4322a' if f['level']=='alto' else '#b07d0a' if f['level']=='medio' else '#1a7a52'};"
        f"padding:8px 12px;border-radius:8px;margin-top:6px;font-size:12.5px'>{f['msg']}</div>"
        for f in out["decay_flags"])

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:16px;background:#f7f6f2;font-family:Georgia,serif;color:#1a1916">
<div style="max-width:600px;margin:0 auto">
  <div style="background:#1e1d1a;color:#e8e6e0;padding:20px 24px;border-radius:12px 12px 0 0">
    <div style="font-size:18px;font-weight:bold">Alocador tático — leitura da semana</div>
    <div style="color:#8a877e;font-size:12px;margin-top:4px">{out.get('asof','')} · regime {m.get('regime','—')}</div>
  </div>
  <div style="background:#fff;border:1px solid #e6e4dd;border-top:none;padding:22px 24px">
    <table style="width:100%"><tr>
      <td style="vertical-align:middle;width:42%">
        <div style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#9a988f">Score · barateza</div>
        <div style="font-size:58px;font-weight:bold;color:{scol};line-height:1">{score if score is not None else '—'}<span style="font-size:20px;color:#bbb">/10</span></div>
        <div style="font-size:11px;color:#9a988f">confiança {out['confidence']}</div>
      </td>
      <td style="vertical-align:middle">
        <div style="background:{d['cor']};color:#fff;padding:14px 18px;border-radius:10px">
          <div style="font-size:26px;font-weight:bold">{d['sinal']}</div>
          <div style="font-size:12px;opacity:.85;margin-top:6px">{d['desc']}</div>
        </div>
      </td>
    </tr></table>
  </div>
  <div style="background:#e8f5ee;border:1px solid #cfe8da;padding:18px 24px;border-radius:12px;margin-top:14px">
    <div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#1a7a52">Deploy desta rodada</div>
    <div style="font-size:30px;font-weight:bold;color:#1a7a52;margin:4px 0">{_brl(d['tranche'])}</div>
    <div style="font-size:12.5px;color:#555">Caixa {cash:.0f}% → piso-alvo {tgt:.0f}% · deploya {b['lambda']*100:.0f}% do gap · munição nunca furada</div>
    <table style="width:100%;margin-top:12px;font-size:13px">{sleeve_rows()}</table>
  </div>
  <div style="background:#fff;border:1px solid #e6e4dd;padding:18px 24px;border-radius:12px;margin-top:14px">
    <table style="width:100%;font-size:12.5px">
      <tr><td>Selic</td><td style="text-align:right">{('%.2f%%'%m['selic']) if m.get('selic') else '—'}</td>
          <td style="padding-left:18px">IPCA 12m</td><td style="text-align:right">{('%.2f%%'%m['ipca']) if m.get('ipca') else '—'}</td></tr>
      <tr><td>CDI real</td><td style="text-align:right">{('%.2f%%'%m['real_cdi']) if m.get('real_cdi') else '—'}</td>
          <td style="padding-left:18px">P/L IBOV</td><td style="text-align:right">{('%.1fx'%m['pl']) if m.get('pl') else '—'}</td></tr>
      <tr><td>IBOV DD do ATH</td><td style="text-align:right">{('%.1f%%'%m['ibov_dd']) if m.get('ibov_dd') is not None else '—'}</td>
          <td style="padding-left:18px">NTN-B longa</td><td style="text-align:right">{('%.2f%%'%m['ntnb_long']) if m.get('ntnb_long') else '—'}</td></tr>
    </table>
  </div>
  <div style="background:#fff;border:1px solid #e6e4dd;padding:18px 24px;border-radius:12px;margin-top:14px">
    <div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#9a988f;margin-bottom:8px">Fatores</div>
    <table style="width:100%;font-size:13px">{groups_rows()}</table>
  </div>
  <div style="background:#fff;border:1px solid #e6e4dd;padding:18px 24px;border-radius:12px;margin-top:14px">
    <div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#9a988f;margin-bottom:4px">Monitor de decay</div>
    {flags}
  </div>
  <div style="color:#9a988f;font-size:11.5px;margin-top:18px;font-style:italic">
    Leitura do material pelo seu framework — não é recomendação; a decisão é sua.
    Caixa {_brl(out['caixa'])} · carteira {_brl(out['carteira_total'])}.
    Banda 5–20% e ritmo λ={b['lambda']:.2f} calibrados em backtest (2014-16 / 2020 / 2021-22 / 2026).
  </div>
</div></body></html>"""
