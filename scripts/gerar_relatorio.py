#!/usr/bin/env python3
"""Gerador automático do relatório de evolução MaisTODOS OKR 3.
Roda via GitHub Actions cron, gera relatorio.html e envia resumo ao Slack.
"""

import os, json, requests, sys
from datetime import date
from collections import defaultdict

# ---------- CONFIG ----------
MB_URL       = os.environ['METABASE_URL'].rstrip('/')
MB_EMAIL     = os.environ['METABASE_EMAIL']
MB_PASSWORD  = os.environ['METABASE_PASSWORD']
SLACK_HOOK   = os.environ.get('SLACK_WEBHOOK_URL', '')
MP_SA_USER   = os.environ.get('MIXPANEL_SA_USER', '')
MP_SA_SECRET = os.environ.get('MIXPANEL_SA_SECRET', '')
DD_API_KEY   = os.environ.get('DD_API_KEY', '')
DD_APP_KEY   = os.environ.get('DD_APP_KEY', '')

HOJE        = date.today()
ANO         = HOJE.year
DB_ACCOUNTS = 39
DB_LAKE     = 41
MP_PROJECT  = '3158031'

MESES_EN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
MESES_PT = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']

# Metabase saved question IDs (monthly component breakdowns)
CARD_IDS = {
    'kr31_cdt': 29359,
    'kr32_pl':  29353,
    'kr33_rd':  29365,
    'kr35_cdt': 29371,
}

# Etapas monitoradas no Accounts DB
STEP_LABELS = {
    'notification_business_bank_profile_created': 'Notif. perfil bancário PJ',
    'notification_personal_bank_profile_created': 'Notif. perfil bancário PF',
    'send_business_bank_profile_for_creation':    'Envio criação perfil bancário',
    'verify_bank_account_already_exists':         'Verificação conta duplicada',
    'onboarding_kyb_automatic':                   'KYB automático',
    'onboarding_kyc':                             'KYC',
    'onboarding_credit':                          'Análise de crédito',
}

# ---------- METABASE ----------
def mb_login():
    r = requests.post(f'{MB_URL}/api/session',
                      json={'username': MB_EMAIL, 'password': MB_PASSWORD},
                      timeout=30)
    r.raise_for_status()
    return r.json()['id']

def mb_sql(token, db_id, sql):
    headers = {'X-Metabase-Session': token, 'Content-Type': 'application/json'}
    body = {'database': db_id, 'type': 'native', 'native': {'query': sql}}
    r = requests.post(f'{MB_URL}/api/dataset', headers=headers, json=body, timeout=90)
    r.raise_for_status()
    d = r.json()['data']
    cols = [c['name'] for c in d['cols']]
    return [dict(zip(cols, row)) for row in d['rows']]

def mb_card(token, card_id):
    headers = {'X-Metabase-Session': token}
    r = requests.post(f'{MB_URL}/api/card/{card_id}/query/json',
                      headers=headers, timeout=90)
    r.raise_for_status()
    return r.json()

# ---------- COLETA DE DADOS ----------
def get_failed_steps(token):
    """Falhas mensais por step code — Accounts DB (DB39)."""
    sql = f"""
    SELECT
      EXTRACT(MONTH FROM s.created_at)::int AS mes_num,
      t.code,
      COUNT(*) AS falhas
    FROM onboarding_companystep s
    JOIN onboarding_steptemplate t ON t.id = s.step_template_id
    WHERE s.status_field = 'failed'
      AND s.deleted_at IS NULL
      AND s.created_at >= '{ANO}-01-01'
      AND s.created_at <  '{ANO+1}-01-01'
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
    rows = mb_sql(token, DB_ACCOUNTS, sql)
    # {code: {mes_num: count}}
    out = defaultdict(lambda: defaultdict(int))
    for r in rows:
        out[r['code']][int(r['mes_num'])] = int(r['falhas'])
    return out

def get_okr_monthly(token):
    """Valores mensais por KR (componentes individuais)."""
    out = {}
    for key, cid in CARD_IDS.items():
        try:
            rows = mb_card(token, cid)
            by_month = {}
            for r in rows:
                m = int(float(r.get('mes_int') or 0))
                v = float(r.get('Valor atual') or 0)
                meta = float(r.get('Meta') or 0)
                if m > 0:
                    by_month[m] = {'valor': v, 'meta': meta}
            out[key] = by_month
        except Exception as e:
            print(f'  [WARN] card {cid} ({key}): {e}', file=sys.stderr)
            out[key] = {}
    return out

def get_mixpanel(sa_user, sa_secret):
    if not sa_user or not sa_secret:
        return {}
    import base64
    auth = base64.b64encode(f'{sa_user}:{sa_secret}'.encode()).decode()
    h = {'Authorization': f'Basic {auth}'}
    out = {}
    for event, key in [('checkout_started', 'started'), ('checkout_completed', 'completed')]:
        try:
            r = requests.get('https://data.mixpanel.com/api/2.0/segmentation',
                             headers=h,
                             params={'project_id': MP_PROJECT, 'event': event,
                                     'from_date': f'{ANO}-01-01',
                                     'to_date': HOJE.isoformat(), 'unit': 'month'},
                             timeout=30)
            r.raise_for_status()
            vals = r.json()['data']['values'][event]
            for dt, cnt in vals.items():
                m = int(dt.split('-')[1])
                if m not in out:
                    out[m] = {}
                out[m][key] = cnt
        except Exception as e:
            print(f'  [WARN] Mixpanel {event}: {e}', file=sys.stderr)
    return out

def get_slos(api_key, app_key):
    if not api_key or not app_key:
        return {}
    h = {'DD-API-KEY': api_key, 'DD-APPLICATION-KEY': app_key}
    try:
        r = requests.get('https://api.datadoghq.com/api/v1/slo',
                         headers=h,
                         params={'query': 'team:app-cartão-de-todos', 'limit': 20},
                         timeout=30)
        r.raise_for_status()
        out = {}
        for slo in r.json().get('data', []):
            name = slo.get('name', '')
            thr  = slo.get('thresholds', [{}])[0]
            stat = slo.get('overall_status', {})
            out[name] = {
                'target':  thr.get('target', 0),
                'current': stat.get('sli_value') or 0,
                'status':  stat.get('status', 'NO_DATA'),
            }
        return out
    except Exception as e:
        print(f'  [WARN] Datadog SLOs: {e}', file=sys.stderr)
        return {}

# ---------- PONTOS DE MELHORIA ----------
def gerar_melhorias(steps, okr, mp, slos, meses_disp):
    items = []
    m_atual = meses_disp[-1] if meses_disp else HOJE.month
    m_ant   = meses_disp[-2] if len(meses_disp) >= 2 else None

    kr_labels = {
        'kr31_cdt': 'KR 3.1 Cashout CDT',
        'kr32_pl':  'KR 3.2 App/Private Label',
        'kr33_rd':  'KR 3.3 Raia Drogasil',
        'kr35_cdt': 'KR 3.5 Transações CDT',
    }
    for key, label in kr_labels.items():
        d = okr.get(key, {})
        if m_atual not in d:
            continue
        v, meta = d[m_atual]['valor'], d[m_atual]['meta']
        pct = v / meta * 100 if meta > 0 else 0

        if pct < 30:
            items.append(('crit', f'🔴 {label}: {pct:.0f}% da meta — nível crítico'))
        elif pct < 50:
            items.append(('warn', f'🟡 {label}: {pct:.0f}% da meta — ação necessária'))

        if m_ant and m_ant in d and d[m_ant]['valor'] > 0:
            delta = (v - d[m_ant]['valor']) / d[m_ant]['valor'] * 100
            if delta < -20:
                items.append(('crit', f'📉 {label}: queda de {abs(delta):.0f}% vs mês anterior'))
            elif delta > 30:
                items.append(('ok', f'📈 {label}: crescimento de +{delta:.0f}% vs mês anterior'))

    for step, months in steps.items():
        v_a  = months.get(m_atual, 0)
        v_b  = months.get(m_ant, 0) if m_ant else 0
        lbl  = STEP_LABELS.get(step, step)
        if v_b > 0 and v_a > 0:
            delta = (v_a - v_b) / v_b * 100
            if delta > 50:
                items.append(('crit', f'🔴 Falhas em {lbl}: +{delta:.0f}% ({v_b}→{v_a})'))
            elif delta < -30:
                items.append(('ok', f'✅ Falhas em {lbl}: −{abs(delta):.0f}% — melhora real'))

    for name, slo in slos.items():
        if slo['current'] > 0 and slo['current'] < slo['target'] - 1:
            items.append(('crit', f'🔴 SLO BREACHED: {name} — {slo["current"]:.2f}% (target {slo["target"]}%)'))
        elif slo['current'] > 0 and slo['current'] < slo['target']:
            items.append(('warn', f'🟡 SLO em risco: {name} — {slo["current"]:.2f}%'))

    if m_atual in mp:
        s = mp[m_atual].get('started', 0)
        c = mp[m_atual].get('completed', 0)
        if s > 0:
            conv = c / s * 100
            if conv < 40:
                items.append(('crit', f'🔴 Conversão checkout: {conv:.1f}% — abaixo de 40%'))

    return sorted(items, key=lambda x: {'crit': 0, 'warn': 1, 'ok': 2}[x[0]])

# ---------- GERAÇÃO DO HTML ----------
def pct_color(pct):
    if pct >= 70: return '#4A9900'
    if pct >= 40: return '#C47000'
    return '#dc2626'

def sparkline(by_month, meses, max_val=None):
    vals = [by_month.get(m, {}).get('valor', 0) if isinstance(by_month.get(m), dict) else by_month.get(m, 0) for m in meses]
    top = max_val or (max(vals) if vals else 1)
    if top == 0: top = 1
    bars = ''
    for i, v in enumerate(vals):
        h = int(v / top * 24)
        mn = MESES_PT[meses[i] - 1]
        bars += f'<div title="{mn}: {v:,.0f}" style="width:10px;height:{h}px;background:#7200D6;border-radius:2px 2px 0 0;flex-shrink:0"></div>'
    return f'<div style="display:flex;align-items:flex-end;gap:2px;height:24px">{bars}</div>'

def td_val(by_month, m):
    v = by_month.get(m, {}).get('valor', None) if isinstance(by_month.get(m), dict) else by_month.get(m, None)
    if v is None: return '<td class="mono" style="color:#ccc;text-align:right">—</td>'
    meta = by_month.get(m, {}).get('meta', 0) if isinstance(by_month.get(m), dict) else 0
    pct = v / meta * 100 if meta > 0 else None
    color = pct_color(pct) if pct is not None else '#888'
    fmt = f'{v:,.0f}' if v >= 1000 else f'{v:.0f}'
    return f'<td class="mono" style="text-align:right;color:{color}">{fmt}</td>'

def td_step(by_month, m):
    v = by_month.get(m, None)
    if v is None: return '<td class="mono" style="color:#ccc;text-align:right">—</td>'
    v_ant = None
    for mm in sorted(by_month.keys()):
        if mm < m:
            v_ant = by_month[mm]
    if v_ant and v_ant > 0:
        delta = (v - v_ant) / v_ant * 100
        color = '#dc2626' if delta > 20 else '#4A9900' if delta < -20 else '#888'
        return f'<td class="mono" style="text-align:right;color:{color}">{v}</td>'
    return f'<td class="mono" style="text-align:right">{v}</td>'

def generate_html(steps, okr, mp, slos, melhorias):
    meses_disp = sorted(set(
        m for d in list(okr.values()) + [mp] for m in d.keys() if isinstance(m, int)
    ) | set(
        m for s in steps.values() for m in s.keys()
    ))
    if not meses_disp:
        meses_disp = list(range(1, HOJE.month + 1))

    headers_mes = ''.join(f'<th>{MESES_PT[m-1]}</th>' for m in meses_disp)
    mes_atual_lbl = MESES_PT[meses_disp[-1] - 1] if meses_disp else MESES_PT[HOJE.month - 1]

    # OKR rows
    kr_rows = ''
    for key, label in [
        ('kr31_cdt', 'KR 3.1 · Cashout CDT'),
        ('kr32_pl',  'KR 3.2 · App/Private Label'),
        ('kr33_rd',  'KR 3.3 · Raia Drogasil'),
        ('kr35_cdt', 'KR 3.5 · Transações CDT'),
    ]:
        d = okr.get(key, {})
        cells = ''.join(td_val(d, m) for m in meses_disp)
        spark = sparkline(d, meses_disp)
        kr_rows += f'<tr><td class="mono">{label}</td>{cells}<td>{spark}</td></tr>\n'

    # Step rows
    step_rows = ''
    for code, lbl in STEP_LABELS.items():
        d = steps.get(code, {})
        if not d:
            continue
        cells = ''.join(td_step(d, m) for m in meses_disp)
        vals = [d.get(m, 0) for m in meses_disp]
        top = max(vals) if vals else 1
        spark = sparkline({m: v for m, v in d.items()}, meses_disp, top)
        step_rows += f'<tr><td class="mono" style="font-size:11px">{lbl}</td>{cells}<td>{spark}</td></tr>\n'

    # Mixpanel rows
    mp_rows = ''
    if mp:
        for key, lbl in [('started', 'Checkout iniciado'), ('completed', 'Checkout concluído')]:
            d_mp = {m: mp[m].get(key, 0) for m in mp}
            cells = ''.join(
                f'<td class="mono" style="text-align:right">{d_mp.get(m, 0):,}</td>' if d_mp.get(m) else '<td class="mono" style="color:#ccc;text-align:right">—</td>'
                for m in meses_disp
            )
            top = max(d_mp.values()) if d_mp else 1
            spark = sparkline(d_mp, meses_disp, top)
            mp_rows += f'<tr><td class="mono" style="font-size:11px">{lbl}</td>{cells}<td>{spark}</td></tr>\n'
        # Conversion row
        conv_cells = ''
        for m in meses_disp:
            s = mp.get(m, {}).get('started', 0)
            c = mp.get(m, {}).get('completed', 0)
            if s > 0:
                pct = c / s * 100
                color = pct_color(pct)
                conv_cells += f'<td class="mono" style="text-align:right;color:{color}">{pct:.1f}%</td>'
            else:
                conv_cells += '<td class="mono" style="color:#ccc;text-align:right">—</td>'
        mp_rows += f'<tr style="font-weight:600"><td class="mono" style="font-size:11px">Taxa de conversão</td>{conv_cells}<td></td></tr>\n'

    # SLO rows
    slo_rows = ''
    for name, slo in slos.items():
        v = slo['current']
        t = slo['target']
        breached = v < t - 1 if v > 0 else False
        color = '#dc2626' if breached else '#4A9900'
        slo_rows += f'<tr><td class="mono" style="font-size:11px">{name}</td><td class="mono" style="color:{color};text-align:right">{v:.2f}%</td><td class="mono" style="text-align:right">{t}%</td><td>{"🔴 BREACHED" if breached else "✅ OK"}</td></tr>\n'

    # Melhorias
    melhoria_html = ''
    for nivel, txt in melhorias:
        cls = 'alert-crit' if nivel == 'crit' else 'alert-warn' if nivel == 'warn' else 'alert-info'
        melhoria_html += f'<div class="alert {cls}" style="margin-bottom:8px">{txt}</div>\n'

    if not melhoria_html:
        melhoria_html = '<div class="alert alert-info">Nenhum ponto crítico identificado neste período.</div>'

    gerado_em = HOJE.strftime('%d/%b/%Y às 8h')

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Relatório OKR 3 · MaisTODOS · {ANO}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --roxo:#7200D6;--verde:#70E000;--mag:#E5087E;
  --bg:#F7F7FA;--bg2:#fff;--text:#1a0033;--text3:#7E7E7E;
  --border:#DDD0F5;--radius:6px;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:Inter,sans-serif;background:var(--bg);color:var(--text);font-size:13px; }}
.gnav {{ position:fixed;top:0;left:0;right:0;height:44px;background:var(--roxo);display:flex;align-items:center;padding:0 20px;gap:8px;z-index:100; }}
.gnav-logo {{ font-weight:700;color:#fff;font-size:14px;letter-spacing:.02em;margin-right:16px; }}
.gnav-pill {{ font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;background:#fff2;color:#fff;font-family:'JetBrains Mono',monospace; }}
.gnav-pill.live {{ background:var(--verde);color:#1a0033; }}
.doc {{ max-width:1080px;margin:0 auto;padding:68px 24px 60px; }}
.header {{ margin-bottom:32px; }}
.eyebrow {{ font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:var(--roxo);margin-bottom:8px; }}
h1 {{ font-size:26px;font-weight:700;line-height:1.2;margin-bottom:8px; }}
.period-chip {{ display:inline-flex;align-items:center;gap:8px;background:var(--bg2);border:1px solid var(--border);border-radius:20px;padding:5px 14px;font-size:12px;font-weight:500;margin-bottom:16px; }}
.atualizado {{ font-size:11px;color:var(--text3);margin-bottom:32px; }}
.section {{ background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:20px 24px;margin-bottom:20px; }}
.section-header {{ display:flex;align-items:center;justify-content:space-between;margin-bottom:16px; }}
.section-title {{ font-weight:600;font-size:14px; }}
.section-sub {{ font-size:11px;color:var(--text3); }}
.table-wrap {{ overflow-x:auto; }}
table {{ width:100%;border-collapse:collapse; }}
th {{ font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text3);padding:8px 10px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap; }}
td {{ padding:7px 10px;border-bottom:1px solid #f0eaf8;vertical-align:middle; }}
tr:last-child td {{ border-bottom:none; }}
.mono {{ font-family:'JetBrains Mono',monospace;font-size:12px; }}
.alert {{ border-radius:6px;padding:10px 14px;font-size:12px;line-height:1.5; }}
.alert-crit {{ background:#fdeaf3;border-left:3px solid #dc2626;color:#7a0020; }}
.alert-warn {{ background:#fff8e1;border-left:3px solid #C47000;color:#7a4400; }}
.alert-info {{ background:#eef2fa;border-left:3px solid var(--roxo);color:var(--text); }}
.footer {{ text-align:center;font-size:11px;color:var(--text3);padding:32px 24px; }}
</style>
</head>
<body>

<div class="gnav">
  <span class="gnav-logo">+maisTODOS</span>
  <span class="gnav-pill">OKR 3</span>
  <span class="gnav-pill live">● atualizado {gerado_em}</span>
</div>

<div class="doc">

  <div class="header">
    <div class="eyebrow">Diretoria de Lealdade · Acompanhamento mensal</div>
    <h1>Evolução OKR 3 — Performance financeira</h1>
    <div class="period-chip">Jan–{mes_atual_lbl} {ANO}</div>
    <p class="atualizado">Gerado automaticamente · {gerado_em} · Accounts DB (DB39) + Metabase Lake (DB41) + Mixpanel (proj. {MP_PROJECT}) + Datadog</p>
  </div>

  <!-- OKR 3 TREND -->
  <div class="section">
    <div class="section-header">
      <span class="section-title">KR 3.1–3.5 · Valores mensais por componente</span>
      <span class="section-sub">Cells coloridas: verde ≥70% meta · amarelo 40–70% · vermelho &lt;40%</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>KR / Componente</th>{headers_mes}<th>Tendência</th></tr></thead>
        <tbody>{kr_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- ACCOUNTS DB STEPS -->
  <div class="section">
    <div class="section-header">
      <span class="section-title">Falhas no onboarding · Accounts DB (DB39)</span>
      <span class="section-sub">Contagem de steps com status failed por mês</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Step</th>{headers_mes}<th>Tendência</th></tr></thead>
        <tbody>{step_rows if step_rows else '<tr><td colspan="20" style="color:#aaa;padding:16px">Sem dados</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  {f'''<!-- MIXPANEL -->
  <div class="section">
    <div class="section-header">
      <span class="section-title">Funil checkout · Mixpanel (proj. {MP_PROJECT})</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Evento</th>{headers_mes}<th>Tendência</th></tr></thead>
        <tbody>{mp_rows}</tbody>
      </table>
    </div>
  </div>''' if mp else ''}

  {f'''<!-- DATADOG SLOs -->
  <div class="section">
    <div class="section-header">
      <span class="section-title">SLOs · Datadog · team:app-cartão-de-todos</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>SLO</th><th style="text-align:right">Atual</th><th style="text-align:right">Target</th><th>Status</th></tr></thead>
        <tbody>{slo_rows}</tbody>
      </table>
    </div>
  </div>''' if slos else ''}

  <!-- PONTOS DE MELHORIA -->
  <div class="section">
    <div class="section-header">
      <span class="section-title">Pontos de melhoria identificados</span>
      <span class="section-sub">Gerados automaticamente via regras de tendência</span>
    </div>
    {melhoria_html}
  </div>

  <div class="footer">
    +maisTODOS · Diretoria de Lealdade · Relatório OKR 3 {ANO} · Atualização automática diária às 8h
  </div>

</div>
</body>
</html>"""

# ---------- SLACK ----------
def enviar_slack(melhorias, mes_atual_lbl):
    if not SLACK_HOOK:
        return
    crits = [t for n, t in melhorias if n == 'crit']
    warns = [t for n, t in melhorias if n == 'warn']
    oks   = [t for n, t in melhorias if n == 'ok']

    linhas = [f'*Relatório OKR 3 · {mes_atual_lbl}/{ANO}* — atualizado agora\n']
    if crits:
        linhas.append('*Críticos:*\n' + '\n'.join(f'> {c}' for c in crits))
    if warns:
        linhas.append('*Atenção:*\n' + '\n'.join(f'> {w}' for w in warns))
    if oks:
        linhas.append('*Destaques positivos:*\n' + '\n'.join(f'> {o}' for o in oks))
    if not crits and not warns:
        linhas.append('✅ Nenhum ponto crítico neste período.')
    linhas.append(f'\n🔗 <https://deploy-diagnostico-gules.vercel.app/relatorio|Ver relatório completo>')

    payload = {'text': '\n\n'.join(linhas)}
    try:
        r = requests.post(SLACK_HOOK, json=payload, timeout=10)
        r.raise_for_status()
        print('Slack: mensagem enviada.')
    except Exception as e:
        print(f'[WARN] Slack: {e}', file=sys.stderr)

# ---------- MAIN ----------
def main():
    print('Autenticando no Metabase...')
    token = mb_login()

    print('Buscando failed steps (Accounts DB)...')
    steps = get_failed_steps(token)

    print('Buscando OKR 3 mensal (Lake DB)...')
    okr = get_okr_monthly(token)

    print('Buscando Mixpanel...')
    mp = get_mixpanel(MP_SA_USER, MP_SA_SECRET)

    print('Buscando Datadog SLOs...')
    slos = get_slos(DD_API_KEY, DD_APP_KEY)

    # Meses disponíveis
    meses_disp = sorted(set(
        m for d in list(okr.values()) + [mp]
        for m in (d.keys() if isinstance(d, dict) else [])
        if isinstance(m, int)
    ) | set(m for s in steps.values() for m in s.keys()))
    if not meses_disp:
        meses_disp = list(range(1, HOJE.month + 1))

    mes_atual_lbl = MESES_PT[meses_disp[-1] - 1] if meses_disp else MESES_PT[HOJE.month - 1]

    print('Gerando pontos de melhoria...')
    melhorias = gerar_melhorias(steps, okr, mp, slos, meses_disp)

    print('Gerando HTML...')
    html = generate_html(steps, okr, mp, slos, melhorias)

    out_path = os.path.join(os.path.dirname(__file__), '..', 'relatorio.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML salvo: {os.path.abspath(out_path)}')

    print('Enviando resumo ao Slack...')
    enviar_slack(melhorias, mes_atual_lbl)

    print('Concluído.')

if __name__ == '__main__':
    main()
