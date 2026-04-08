"""
Six Thinking Hats Arena — AI-powered structured thinking tool.
Based on Edward de Bono's Six Thinking Hats methodology.
Run: python hats_app.py  |  Open: http://localhost:5001
"""

from flask import Flask, Response, render_template_string, request, jsonify, redirect, session
import anthropic
import json
import os
import re
import smtplib
import threading
import time
import webbrowser
from io import BytesIO
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'hats-arena-dev-key-change-in-prod')

MODEL = "claude-opus-4-6"

# ── Hat definitions ───────────────────────────────────────────────────────────

HAT_ORDER = ['white', 'red', 'yellow', 'black', 'green', 'blue']

HATS = {
    'white':  {'label': 'White Hat',  'desc': 'Facts & Data',          'emoji': '⬜'},
    'red':    {'label': 'Red Hat',    'desc': 'Emotions & Intuition',  'emoji': '🔴'},
    'yellow': {'label': 'Yellow Hat', 'desc': 'Optimism & Benefits',   'emoji': '🟡'},
    'black':  {'label': 'Black Hat',  'desc': 'Caution & Risks',       'emoji': '⬛'},
    'green':  {'label': 'Green Hat',  'desc': 'Creativity & Ideas',    'emoji': '🟢'},
    'blue':   {'label': 'Blue Hat',   'desc': 'Process & Overview',    'emoji': '🔵'},
}

HAT_SYSTEMS = {
    'white': (
        "You are the White Hat thinker in a Six Thinking Hats session. "
        "Focus purely on facts, data, and objective information relevant to the topic. "
        "What do we know? What information is available? What are the verifiable facts? "
        "Stay completely neutral — no opinions, emotions, or speculation."
    ),
    'red': (
        "You are the Red Hat thinker in a Six Thinking Hats session. "
        "Express gut feelings, emotions, and intuitions about the topic. "
        "You do not need to justify your feelings — just express them honestly and directly. "
        "This is pure instinct and emotional response, not rational argument."
    ),
    'yellow': (
        "You are the Yellow Hat thinker in a Six Thinking Hats session. "
        "Think optimistically and constructively. Focus on the positives, benefits, "
        "opportunities, and all the reasons why this could succeed or be valuable. "
        "Be genuinely enthusiastic — find and articulate the real value."
    ),
    'black': (
        "You are the Black Hat thinker in a Six Thinking Hats session. "
        "Be the critical voice. Identify risks, problems, difficulties, flaws, "
        "and reasons why things might fail or cause harm. "
        "This is logical caution and rigorous critical analysis — not pessimism for its own sake."
    ),
    'green': (
        "You are the Green Hat thinker in a Six Thinking Hats session. "
        "Think creatively and laterally. Generate new ideas, alternatives, possibilities, "
        "and creative solutions. Challenge assumptions, make unexpected connections, "
        "and propose things no one else has thought of."
    ),
    'blue': (
        "You are the Blue Hat thinker in a Six Thinking Hats session. "
        "Take the meta-view. Reflect on the big picture — what has been covered, "
        "what is missing, what patterns emerge across all the thinking so far, "
        "and what conclusions or next steps are becoming clear."
    ),
}

JUDGE_SYSTEM = (
    "You are a synthesis facilitator for a Six Thinking Hats thinking session. "
    "Your role is to evaluate all perspectives and synthesise them into clear, "
    "actionable insights. Be decisive, specific, and genuinely helpful. "
    "Identify the most valuable contributions without bias."
)

SOURCE_RULE = (
    "After your point, on a new line write **Sources:** followed by 1–2 markdown links "
    "to real, verifiable sources: [Description](https://url.com). "
    "Only use URLs from reputable sites (Reuters, BBC, Nature, WHO, government sites, Wikipedia). "
    "If unsure of a specific article URL, link to the publication's homepage instead."
)

# ── Prompts ───────────────────────────────────────────────────────────────────

def hat_prompt(question, hat_id, round_num, prev_points=None, peer_points=None):
    extra = (
        "Make your opening point on this topic."
        if round_num == 1
        else "Introduce a completely new and distinct point — different from everything you have already said."
    )
    prev_block = ""
    if prev_points:
        prev_block = (
            f"\n\nYour PREVIOUS points in this session:\n{prev_points}\n\n"
            "You MUST make a substantively different point — "
            "do NOT repeat, rephrase, or echo any of the above."
        )
    peer_block = ""
    if peer_points:
        peer_block = (
            f"\n\nOther perspectives already shared this round:\n{peer_points}\n\n"
            "You may acknowledge, build on, or contrast with these — "
            "or take an entirely independent direction. The choice is yours."
        )
    return (
        f'Topic: "{question}"\n\n'
        f"Round {round_num}: {extra}{prev_block}{peer_block}\n\n"
        f"State your point in 60 words or fewer, then add sources.\n{SOURCE_RULE}"
    )

def build_judge_prompt(question, transcript_data, active_hats):
    sections = []
    total_rounds = max((t.get("round", 0) for t in transcript_data), default=0)
    for hat_id in active_hats:
        hat_turns = [t for t in transcript_data if t.get("hat") == hat_id]
        if hat_turns:
            info   = HATS[hat_id]
            points = "\n".join(f"  Round {t.get('round','?')}: {t['text']}" for t in hat_turns)
            sections.append(f"=== {info['label']} — {info['desc']} ===\n{points}")
    return (
        f'Topic: "{question}"\n\n'
        f"A Six Thinking Hats session ran {total_rounds} round(s) "
        f"with {len(active_hats)} hat(s):\n\n"
        + "\n\n".join(sections)
        + "\n\nIn 2–3 sentences each, provide:\n"
        "1. The single strongest insight from each hat\n"
        "2. One overall recommendation from the combined perspectives\n"
        "3. Which hat was most decisive, and why (one sentence)\n\n"
        "Be concise. Then on the very last line write exactly:\n"
        "MVP: hat_id\n"
        "(Replace hat_id with one of: white, red, yellow, black, green, blue)"
    )

# ── Routes ────────────────────────────────────────────────────────────────────

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# ── PDF ───────────────────────────────────────────────────────────────────────

GDRIVE_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gdrive_token.json')
GDRIVE_SCOPES     = ['https://www.googleapis.com/auth/drive.file']

HAT_PDF_COLORS = {
    'white': '#9ca3af', 'red': '#ef4444', 'yellow': '#f59e0b',
    'black': '#6b7280', 'green': '#10b981', 'blue':  '#3b82f6',
}

def _strip_md(text):
    """Convert markdown to ReportLab-compatible HTML (supports clickable links)."""
    # Escape raw HTML chars first (except we'll add our own tags below)
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    # Bold and italic
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
    # Clickable links [label](url) → ReportLab anchor
    text = re.sub(
        r'\[(.+?)\]\((https?://[^\)]+)\)',
        r'<a href="\2" color="#3b82f6">\1</a>',
        text
    )
    # Headings → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # Bullets
    text = re.sub(r'^[-*]\s+', '\u2022 ', text, flags=re.MULTILINE)
    return text.strip()

def build_pdf_bytes(question, rounds, active_hats, mvp_hat, verdict_text, transcript):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib.colors import HexColor, black

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2.5*cm, rightMargin=2.5*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    def sty(name, **kw):
        defaults = dict(fontName='Helvetica', fontSize=9.5, leading=14,
                        textColor=HexColor('#333333'), spaceAfter=4)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    s_title  = sty('T', fontSize=20, leading=26, fontName='Helvetica-Bold',
                   textColor=HexColor('#1a1a2e'), spaceAfter=3)
    s_sub    = sty('S', fontSize=8,  textColor=HexColor('#888888'), spaceAfter=10)
    s_label  = sty('L', fontSize=7,  textColor=HexColor('#888888'),
                   fontName='Helvetica', spaceAfter=2, leading=9)
    s_topic  = sty('Q', fontSize=13, leading=17, fontName='Helvetica-Bold',
                   textColor=HexColor('#1a1a2e'), spaceAfter=6)
    s_meta   = sty('M', fontSize=8.5, textColor=HexColor('#555555'), spaceAfter=8)
    s_body   = sty('B', fontSize=9.5, leading=14, spaceAfter=5)
    s_round  = sty('R', fontSize=7.5, textColor=HexColor('#888888'), spaceAfter=2, leading=10)
    s_footer = sty('F', fontSize=7.5, textColor=HexColor('#aaaaaa'),
                   fontName='Helvetica-Oblique')

    story = []
    hr  = lambda: HRFlowable(width='100%', thickness=0.8, color=HexColor('#dddddd'), spaceAfter=8)
    sp  = lambda n=6: Spacer(1, n)
    add = story.append
    add(Paragraph('Six Thinking Hats Arena', s_title))
    add(Paragraph('\u00a9 2026 BGAD Consulting \u00b7 bgadconsulting.com', s_sub))
    add(hr())
    add(Paragraph('TOPIC', s_label))
    add(Paragraph(question or '\u2014', s_topic))

    hat_names = ', '.join(HATS[h]['label'] for h in active_hats if h in HATS)
    add(Paragraph(f"{rounds} round{'s' if rounds != 1 else ''} \u00b7 {hat_names}", s_meta))

    if mvp_hat and mvp_hat in HATS:
        info  = HATS[mvp_hat]
        color = HexColor(HAT_PDF_COLORS.get(mvp_hat, '#888888'))
        s_mvp = sty('MVP', fontSize=10, fontName='Helvetica-Bold',
                    textColor=color, spaceAfter=8,
                    borderColor=color, borderWidth=1, borderPadding=6)
        add(Paragraph(
            f"Most Valuable Hat: {info['emoji']} {info['label']} \u2014 {info['desc']}", s_mvp))

    add(sp(8)); add(hr())
    add(Paragraph('SYNTHESIS', s_label))
    for para in _strip_md(verdict_text).split('\n\n'):
        para = para.strip()
        if para:
            add(Paragraph(para.replace('\n', ' '), s_body))

    add(sp(12)); add(hr())
    add(Paragraph('FULL TRANSCRIPT', s_label))
    add(sp(6))

    for hat_id in active_hats:
        turns = [t for t in transcript if t.get('hat') == hat_id]
        if not turns:
            continue
        info  = HATS.get(hat_id, {})
        color = HexColor(HAT_PDF_COLORS.get(hat_id, '#888888'))
        s_hat = sty('H' + hat_id, fontSize=11, fontName='Helvetica-Bold',
                    textColor=color, spaceAfter=4, leading=14)
        add(Paragraph(f"{info.get('emoji','')} {info.get('label', hat_id)} \u2014 {info.get('desc','')}", s_hat))
        for t in turns:
            add(Paragraph(f"Round {t.get('round', '?')}", s_round))
            for line in _strip_md(t.get('text', '')).split('\n'):
                line = line.strip()
                if line:
                    add(Paragraph(line, s_body))
            add(sp(4))
        add(sp(8))

    add(hr())
    add(Paragraph(
        'Verify all cited sources independently. '
        'Generated by Six Thinking Hats Arena \u00b7 bgadconsulting.com', s_footer))

    doc.build(story)
    buf.seek(0)
    return buf.read()

# ── Google Drive helpers ──────────────────────────────────────────────────────

def _gdrive_config():
    cid  = os.environ.get('GOOGLE_CLIENT_ID',     '')
    csec = os.environ.get('GOOGLE_CLIENT_SECRET', '')
    if cid and csec:
        return {"web": {"client_id": cid, "client_secret": csec,
                        "auth_uri":  "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token"}}
    cf = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'google_credentials.json')
    if os.path.exists(cf):
        with open(cf) as f:
            return json.load(f)
    return None

def _gdrive_redirect_uri():
    base = os.environ.get('BASE_URL', 'http://localhost:5001')
    return base.rstrip('/') + '/gdrive/callback'

def _load_gdrive_creds():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        if not os.path.exists(GDRIVE_TOKEN_FILE):
            return None
        creds = Credentials.from_authorized_user_file(GDRIVE_TOKEN_FILE, GDRIVE_SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
            with open(GDRIVE_TOKEN_FILE, 'w') as f:
                f.write(creds.to_json())
            return creds
    except Exception:
        pass
    return None

# ── Google Drive routes ───────────────────────────────────────────────────────

@app.route('/gdrive/status')
def gdrive_status():
    cfg = _gdrive_config()
    if not cfg:
        return jsonify({'configured': False, 'authenticated': False})
    return jsonify({'configured': True, 'authenticated': _load_gdrive_creds() is not None})

@app.route('/gdrive/auth')
def gdrive_auth():
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        return 'google-auth-oauthlib not installed', 500
    cfg = _gdrive_config()
    if not cfg:
        return 'Google Drive not configured', 400
    flow = Flow.from_client_config(cfg, scopes=GDRIVE_SCOPES,
                                    redirect_uri=_gdrive_redirect_uri())
    auth_url, state = flow.authorization_url(access_type='offline', prompt='consent')
    session['gdrive_state'] = state
    return redirect(auth_url)

@app.route('/gdrive/callback')
def gdrive_callback():
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        return 'google-auth-oauthlib not installed', 500
    cfg = _gdrive_config()
    if not cfg:
        return 'Not configured', 400
    flow = Flow.from_client_config(cfg, scopes=GDRIVE_SCOPES,
                                    redirect_uri=_gdrive_redirect_uri(),
                                    state=session.get('gdrive_state'))
    flow.fetch_token(authorization_response=request.url)
    with open(GDRIVE_TOKEN_FILE, 'w') as f:
        f.write(flow.credentials.to_json())
    return (
        '<html><body>'
        '<script>if(window.opener){window.opener.postMessage("gdrive_ok","*");window.close();}</script>'
        '<p style="font-family:sans-serif;padding:20px">&#10003; Authenticated! You can close this window.</p>'
        '</body></html>'
    )

@app.route('/generate_pdf', methods=['POST'])
def generate_pdf_route():
    body = request.get_json(force=True, silent=True) or {}
    try:
        pdf = build_pdf_bytes(
            body.get('question', ''), body.get('rounds', 0),
            body.get('hats', []),     body.get('mvpHat', ''),
            body.get('verdictText', ''), body.get('transcript', [])
        )
        fname = re.sub(r'[^\w\s-]', '', body.get('question', 'session'))[:40].strip() + '.pdf'
        return Response(pdf, mimetype='application/pdf',
                        headers={'Content-Disposition': f'attachment; filename="{fname}"'})
    except ImportError:
        return jsonify({'error': 'reportlab not installed — run: pip install reportlab'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/gdrive/upload', methods=['POST'])
def gdrive_upload():
    body = request.get_json(force=True, silent=True) or {}
    try:
        from googleapiclient.discovery import build as gbuild
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError:
        return jsonify({'error': 'Run: pip install google-api-python-client google-auth-oauthlib'}), 500
    creds = _load_gdrive_creds()
    if not creds:
        return jsonify({'error': 'Not authenticated with Google Drive', 'need_auth': True}), 401
    try:
        pdf = build_pdf_bytes(
            body.get('question', ''), body.get('rounds', 0),
            body.get('hats', []),     body.get('mvpHat', ''),
            body.get('verdictText', ''), body.get('transcript', [])
        )
        fname   = re.sub(r'[^\w\s-]', '', body.get('question', 'session'))[:40].strip() + '.pdf'
        service = gbuild('drive', 'v3', credentials=creds)
        meta    = {'name': fname, 'mimeType': 'application/pdf'}
        media   = MediaIoBaseUpload(BytesIO(pdf), mimetype='application/pdf')
        f       = service.files().create(body=meta, media_body=media,
                                         fields='id,webViewLink').execute()
        return jsonify({'ok': True, 'link': f.get('webViewLink', ''), 'name': fname})
    except ImportError:
        return jsonify({'error': 'Run: pip install reportlab'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/logo")
def serve_logo():
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    if not os.path.exists(logo_path):
        return "", 404
    with open(logo_path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "max-age=86400"})

@app.route("/think", methods=["POST"])
def think():
    body             = request.get_json(force=True, silent=True) or {}
    question         = body.get("question",        "").strip()
    round_num        = int(body.get("round",       1))
    hats             = body.get("hats",            [])
    transcript       = body.get("transcript",      [])
    cross_pollinate  = body.get("cross_pollinate", False)

    def generate():
        try:
            ordered_hats  = [h for h in hats if h in HAT_SYSTEMS]
            round_so_far  = []   # (hat_id, text) for hats that have spoken this round

            for hat_id in ordered_hats:
                prev_pts_list = [t["text"] for t in transcript if t.get("hat") == hat_id]
                prev_pts = "\n\n---\n\n".join(prev_pts_list) if prev_pts_list else None

                peer_pts = None
                if cross_pollinate and round_so_far:
                    peer_pts = "\n\n".join(
                        f"{HATS[h]['label']}: {text}" for h, text in round_so_far
                    )

                prompt = hat_prompt(question, hat_id, round_num, prev_pts, peer_pts)

                yield f"data: {json.dumps({'type':'turn_start','hat':hat_id,'round':round_num})}\n\n"

                full_text = ""
                try:
                    with client.messages.stream(
                        model=MODEL, max_tokens=350,
                        system=HAT_SYSTEMS[hat_id],
                        messages=[{"role": "user", "content": prompt}],
                    ) as stream:
                        for text in stream.text_stream:
                            full_text += text
                            yield f"data: {json.dumps({'type':'chunk','hat':hat_id,'text':text})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"
                    return

                yield f"data: {json.dumps({'type':'turn_end','hat':hat_id,'round':round_num,'text':full_text})}\n\n"
                round_so_far.append((hat_id, full_text))

            yield f"data: {json.dumps({'type':'round_done','round':round_num})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/judge_hats", methods=["POST"])
def judge_hats():
    body        = request.get_json(force=True, silent=True) or {}
    question    = body.get("question",    "").strip()
    transcript  = body.get("transcript",  [])
    active_hats = body.get("hats",        [])
    prompt      = build_judge_prompt(question, transcript, active_hats)

    def generate():
        try:
            full_text = ""
            with client.messages.stream(
                model=MODEL, max_tokens=1100,
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    full_text += text
                    yield f"data: {json.dumps({'type':'chunk','text':text})}\n\n"

            # Extract MVP from the last non-empty line ("MVP: hat_id")
            mvp_hat = ''
            for line in reversed(full_text.strip().splitlines()):
                clean = line.strip().lower()
                if clean.startswith('mvp:'):
                    candidate = clean[4:].strip()
                    if candidate in HAT_SYSTEMS:
                        mvp_hat = candidate
                    break

            # Strip the MVP line from the displayed verdict text
            lines = full_text.strip().splitlines()
            if lines and lines[-1].strip().lower().startswith('mvp:'):
                display_text = '\n'.join(lines[:-1]).strip()
            else:
                display_text = full_text.strip()

            yield f"data: {json.dumps({'type':'mvp','hat':mvp_hat,'display':display_text})}\n\n"
            yield f"data: {json.dumps({'type':'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/send_email", methods=["POST"])
def send_email():
    body        = request.get_json(force=True, silent=True) or {}
    to_email    = body.get("to_email",    "").strip()
    question    = body.get("question",    "").strip()
    rounds      = body.get("rounds",      0)
    mvp_hat     = body.get("mvpHat",      "")
    verdict_txt = body.get("verdictText", "")
    transcript  = body.get("transcript",  [])
    active_hats = body.get("hats",        [])

    if not to_email:
        return jsonify({"error": "No recipient email provided"}), 400

    smtp_user = body.get("smtp_user", "").strip() or os.environ.get("SMTP_USER", "")
    smtp_pass = body.get("smtp_pass", "").strip() or os.environ.get("SMTP_PASS", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not smtp_user or not smtp_pass:
        return jsonify({"error": "Please enter your Gmail address and App Password."}), 400

    mvp_info  = HATS.get(mvp_hat, {})
    mvp_label = f"{mvp_info.get('emoji','')} {mvp_info.get('label','N/A')}" if mvp_info else "N/A"

    HAT_COLORS_EMAIL = {
        'white': '#9ca3af', 'red': '#ef4444', 'yellow': '#f59e0b',
        'black': '#6b7280', 'green': '#10b981', 'blue': '#3b82f6',
    }

    trans_lines = []
    for hat_id in active_hats:
        hat_turns = [t for t in transcript if t.get("hat") == hat_id]
        if hat_turns:
            info = HATS.get(hat_id, {})
            trans_lines.append(f"\n=== {info.get('label', hat_id)} ===")
            for t in hat_turns:
                trans_lines.append(f"  Round {t['round']}: {t['text']}")

    plain_body = (
        f"SIX THINKING HATS — SESSION SUMMARY\n"
        f"© 2026 BGAD Consulting · bgadconsulting.com\n\n"
        f"Topic:  {question}\n"
        f"Rounds: {rounds}\n"
        f"Most Valuable Hat: {mvp_label}\n\n"
        f"{'─'*50}\nSYNTHESIS\n{'─'*50}\n{verdict_txt}\n\n"
        f"{'─'*50}\nFULL TRANSCRIPT\n{'─'*50}\n" + "\n".join(trans_lines) + "\n\n"
        f"Verify all cited sources independently.\n"
        f"Sent by Six Thinking Hats Arena · bgadconsulting.com"
    )

    def esc(s):
        return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br>")

    trans_html = []
    for hat_id in active_hats:
        hat_turns = [t for t in transcript if t.get("hat") == hat_id]
        if hat_turns:
            info  = HATS.get(hat_id, {})
            color = HAT_COLORS_EMAIL.get(hat_id, '#888')
            trans_html.append(
                f'<h3 style="color:{color};margin:16px 0 8px">'
                f'{info.get("emoji","")}&nbsp;{info.get("label", hat_id)}</h3>'
            )
            for t in hat_turns:
                trans_html.append(
                    f'<div style="margin-bottom:8px;padding:10px 14px;background:#111124;'
                    f'border-radius:7px;border-left:3px solid {color}">'
                    f'<div style="font-size:.68rem;color:{color};font-weight:700;margin-bottom:4px">'
                    f'Round {t["round"]}</div>'
                    f'<div style="font-size:.85rem;color:#c8c8d8">{esc(t["text"])}</div></div>'
                )

    mvp_color = HAT_COLORS_EMAIL.get(mvp_hat, '#888')
    r_label   = f"{rounds} round{'s' if rounds != 1 else ''}"
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="background:#0d0d1a;color:#e0e0e0;font-family:Segoe UI,Arial,sans-serif;margin:0;padding:20px">
<div style="max-width:660px;margin:0 auto">
  <div style="background:linear-gradient(135deg,#12122a,#1a1a3a);padding:16px 22px;
              border-radius:12px 12px 0 0;border:1px solid #2a2a4a;
              display:flex;align-items:center;justify-content:space-between">
    <div>
      <div style="font-size:1.05rem;font-weight:800;color:#e0e0e0">Six Thinking Hats Arena</div>
      <div style="font-size:.68rem;color:#555;margin-top:2px">Session Summary</div>
    </div>
    <a href="https://www.bgadconsulting.com" style="color:#60a5fa;font-size:.72rem;text-decoration:none">
      © 2026 BGAD Consulting</a>
  </div>
  <div style="background:#13131f;padding:20px 22px;border:1px solid #2a2a4a;border-top:none;
              border-radius:0 0 12px 12px">
    <p style="font-size:.72rem;color:#555;margin:0 0 4px">TOPIC</p>
    <h2 style="color:#e0e0e0;font-size:1rem;margin:0 0 14px">{esc(question)}</h2>
    <div style="margin-bottom:18px">
      <span style="background:#1e1e30;color:#888;padding:4px 12px;border-radius:9px;
                   font-size:.72rem;margin-right:10px">{r_label}</span>
      <span style="color:{mvp_color};font-weight:800;font-size:.85rem">MVP: {esc(mvp_label)}</span>
    </div>
    <div style="background:#0f0f1e;border:1px solid #2a2a40;border-radius:9px;
                padding:16px;margin-bottom:20px">
      <p style="font-size:.68rem;font-weight:800;letter-spacing:2px;color:#fbbf24;
                text-transform:uppercase;margin:0 0 10px">Synthesis</p>
      <div style="font-size:.85rem;color:#c8c8d8;line-height:1.65">{esc(verdict_txt)}</div>
    </div>
    <p style="font-size:.68rem;font-weight:800;letter-spacing:2px;color:#93c5fd;
              text-transform:uppercase;margin:0 0 10px">Session Transcript</p>
    {''.join(trans_html)}
    <p style="font-size:.65rem;color:#3a3a5a;margin-top:16px;font-style:italic">
      Verify all cited sources independently.
      Sent by Six Thinking Hats Arena ·
      <a href="https://www.bgadconsulting.com" style="color:#60a5fa">bgadconsulting.com</a></p>
  </div>
</div>
</body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Hats Session: {question[:75]}"
        msg["From"]    = smtp_user
        msg["To"]      = to_email
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body,  "html"))
        with smtplib.SMTP(smtp_host, smtp_port) as srv:
            srv.ehlo(); srv.starttls(); srv.login(smtp_user, smtp_pass)
            srv.sendmail(smtp_user, to_email, msg.as_string())
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ── HTML Template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Six Thinking Hats Arena</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background: #0d0d1a; color: #e0e0e0;
    height: 100vh; display: flex; flex-direction: column; overflow: hidden;
}

/* ── Hat CSS variables ── */
[data-hat="white"]  { --hc:#e5e7eb; --hbg:#18181e; --ha:#9ca3af; --hbd:#3a3a4a; --hhdr:#1e1e28; }
[data-hat="red"]    { --hc:#fca5a5; --hbg:#180505; --ha:#ef4444; --hbd:#7f1d1d; --hhdr:#220808; }
[data-hat="yellow"] { --hc:#fde68a; --hbg:#181200; --ha:#f59e0b; --hbd:#78350f; --hhdr:#221900; }
[data-hat="black"]  { --hc:#d1d5db; --hbg:#0e0e0e; --ha:#6b7280; --hbd:#2d2d2d; --hhdr:#141414; }
[data-hat="green"]  { --hc:#6ee7b7; --hbg:#001208; --ha:#10b981; --hbd:#065f46; --hhdr:#001a0a; }
[data-hat="blue"]   { --hc:#93c5fd; --hbg:#000518; --ha:#3b82f6; --hbd:#1e3a8a; --hhdr:#000822; }

/* ── Header ── */
header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 18px; background: #0d0d1a;
    border-bottom: 1px solid #1a1a2e; flex-shrink: 0;
}
.logo      { font-size: 1.05rem; font-weight: 800; color: #e0e0e0; letter-spacing: -0.3px; }
.logo-sub  { font-size: 0.62rem; color: #555; margin-top: 1px; }
.model-badge {
    font-size: 0.62rem; color: #4a8adc; background: #0d1a2e;
    padding: 2px 9px; border-radius: 9px; border: 1px solid #1a3a5c;
}
.brand-link { font-size: 0.7rem; color: #4a8adc; text-decoration: none; opacity: 0.7; transition: opacity 0.15s; }
.brand-link:hover { opacity: 1; }
.brand-logo { height: 28px; width: auto; opacity: 0.9; }

/* ── Hat selector ── */
.hat-selector {
    display: flex; gap: 6px; padding: 8px 16px; justify-content: center;
    background: #0d0d1a; border-bottom: 1px solid #1a1a2e; flex-shrink: 0;
    flex-wrap: wrap;
}
.hat-toggle {
    display: flex; flex-direction: column; align-items: center;
    padding: 6px 12px; border-radius: 10px; cursor: pointer;
    border: 2px solid var(--hbd, #2a2a40); background: transparent;
    opacity: 0.35; transition: all 0.18s; min-width: 80px; user-select: none;
}
.hat-toggle:hover { opacity: 0.65; }
.hat-toggle.on { opacity: 1; background: var(--hbg); border-color: var(--ha); }
.hat-toggle.disabled { pointer-events: none; }
.ht-emoji { font-size: 1.25rem; }
.ht-name  { font-size: 0.7rem; font-weight: 700; color: var(--hc, #e0e0e0); margin-top: 2px; }
.ht-desc  { font-size: 0.57rem; color: #777; }
.cross-toggle {
    display: flex; align-items: center; gap: 8px; cursor: pointer;
    padding: 6px 12px; border-radius: 10px; border: 2px solid #2a2a40;
    background: #111128; transition: all 0.18s; user-select: none;
    font-size: 0.7rem; color: #555; white-space: nowrap; align-self: center;
}
.cross-toggle:hover { border-color: #4a5568; color: #888; }
.cross-toggle.on { border-color: #f59e0b; background: #1a1400; color: #fde68a; }
.cross-toggle input { display: none; }
.cross-pill {
    font-size: 0.58rem; font-weight: 800; letter-spacing: 0.5px;
    padding: 2px 6px; border-radius: 4px;
    background: #2a2a40; color: #444; transition: all 0.18s;
}
.cross-toggle.on .cross-pill { background: #f59e0b; color: #1a0f00; }

/* ── Chairman mode ── */
.chairman-bar {
    display: flex; align-items: center; gap: 10px; padding: 8px 16px;
    background: #0a0a18; border-bottom: 2px solid #2a1f6e; flex-shrink: 0; flex-wrap: wrap;
}
.chairman-label { font-size: 0.72rem; color: #9ca3af; white-space: nowrap; font-weight: 600; letter-spacing: 0.3px; }
.chairman-hats  { display: flex; gap: 7px; flex-wrap: wrap; }
.chairman-hat-btn {
    display: flex; align-items: center; gap: 5px; cursor: pointer;
    padding: 5px 13px; border-radius: 20px;
    font-size: 0.75rem; font-weight: 700;
    border: 1.5px solid var(--ha, #555); color: var(--hc, #ccc);
    background: var(--hbg, #111); transition: opacity 0.15s, transform 0.1s, box-shadow 0.15s;
    user-select: none;
}
.chairman-hat-btn:hover { opacity: 0.85; transform: translateY(-1px); box-shadow: 0 3px 10px rgba(0,0,0,0.4); }
.chairman-hat-btn:active { transform: translateY(0); }
.rapid-disabled { opacity: 0.35 !important; pointer-events: none !important; }

/* ── Input bar ── */
.input-bar {
    padding: 8px 16px; border-bottom: 1px solid #1a1a2e;
    background: #0d0d1a; flex-shrink: 0;
}
.input-row { display: flex; gap: 8px; max-width: 960px; margin: 0 auto; align-items: center; }
.input-row input[type="text"] {
    flex: 1; padding: 9px 14px; border-radius: 10px;
    border: 1px solid #2a2a40; background: #111128; color: #e0e0e0;
    font-size: 0.88rem; outline: none;
}
.input-row input[type="text"]:focus { border-color: #4a8adc; }
.rapid-rounds-input {
    width: 52px; padding: 6px 8px; border-radius: 8px;
    border: 1px solid #3a3a5c; background: #1a1a2e; color: #e0e0e0;
    font-size: 0.85rem; text-align: center;
}
.rapid-rounds-input:focus { outline: none; border-color: #6d28d9; }
.rapid-rounds-label { font-size: 0.78rem; color: #888; white-space: nowrap; }

/* ── Buttons ── */
.btn {
    padding: 8px 16px; border-radius: 10px; border: none; cursor: pointer;
    font-size: 0.8rem; font-weight: 600; transition: all 0.15s; white-space: nowrap;
}
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
.btn-primary { background: linear-gradient(135deg,#1a3a6a,#2563eb); color: #bfdbfe; }
.btn-rapid   { background: linear-gradient(135deg,#4c1d95,#6d28d9); color: #ddd6fe; }
.btn-next    { background: linear-gradient(135deg,#064e3b,#059669); color: #a7f3d0; }
.btn-judge   { background: linear-gradient(135deg,#78350f,#d97706); color: #fef3c7; }
.btn-reset   { background: #1e1e30; color: #a0a0b0; border: 1px solid #2a2a40; }
.btn-rejudge { background: linear-gradient(135deg,#1e1b4b,#4338ca); color: #c7d2fe; }
.btn-email   { background: linear-gradient(135deg,#1a3a2a,#15803d); color: #bbf7d0; }
.btn-hist    { background: #1e1e30; color: #a0aec0; border: 1px solid #2a2a40; font-size: 0.78rem; }
.btn-pdf     { background: linear-gradient(135deg,#1a1a3a,#7c3aed); color: #ddd6fe; }
.hidden { display: none !important; }

/* ── PDF / Drive modal ── */
.pdf-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: flex; align-items: center; justify-content: center; z-index: 200;
}
.pdf-modal {
    background: #111124; border: 1px solid #2a2a4a; border-radius: 14px;
    width: min(420px, 95vw); box-shadow: 0 20px 60px rgba(0,0,0,0.6);
}
.pdf-hdr   { padding: 16px 20px 8px; border-bottom: 1px solid #1a1a3a; }
.pdf-title { font-size: 0.9rem; font-weight: 700; color: #e0e0e0; }
.pdf-desc  { font-size: 0.72rem; color: #555; margin-top: 3px; }
.pdf-body  { padding: 16px 20px; display: flex; flex-direction: column; gap: 10px; }
.pdf-option {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 16px; border-radius: 10px; background: #0d0d1a;
    border: 1px solid #2a2a40;
}
.pdf-opt-info { display: flex; flex-direction: column; gap: 2px; }
.pdf-opt-name { font-size: 0.85rem; font-weight: 600; color: #e0e0e0; }
.pdf-opt-desc { font-size: 0.68rem; color: #666; }
.pdf-status   { font-size: 0.72rem; min-height: 1.2em; padding: 0 4px; }
.pdf-status.ok  { color: #4ade80; }
.pdf-status.err { color: #f87171; }
.pdf-status.inf { color: #60a5fa; }
.pdf-footer { padding: 10px 20px; border-top: 1px solid #1a1a3a; display: flex; justify-content: flex-end; }
.drive-auth-row { font-size: 0.68rem; color: #888; display: flex; align-items: center; gap: 6px; }
.drive-auth-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }

/* ── Controls bar ── */
.controls-bar {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    padding: 5px 14px; background: #0a0a16; border-bottom: 1px solid #1a1a2e;
    flex-shrink: 0;
}
.ctrl-question { font-size: 0.75rem; color: #888; flex: 1; min-width: 0;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctrl-question strong { color: #c0c0d8; }
.rapid-badge {
    font-size: 0.6rem; font-weight: 800; letter-spacing: 1px; padding: 2px 9px;
    border-radius: 9px; background: #2d1a4a; color: #c084fc; text-transform: uppercase;
    flex-shrink: 0; white-space: nowrap;
}
.ctrl-status { font-size: 0.72rem; padding: 2px 10px; border-radius: 8px; white-space: nowrap; flex-shrink: 0; }
.ctrl-status.idle { background: #1a1a2e; color: #6060a0; }
.ctrl-status.done { background: #0f2a0f; color: #4ade80; }
.ctrl-status.white  { background: #1e1e28; color: #9ca3af; }
.ctrl-status.red    { background: #220808; color: #fca5a5; }
.ctrl-status.yellow { background: #221900; color: #fde68a; }
.ctrl-status.black  { background: #141414; color: #d1d5db; }
.ctrl-status.green  { background: #001a0a; color: #6ee7b7; }
.ctrl-status.blue   { background: #000822; color: #93c5fd; }
.progress-wrap {
    height: 4px; flex: 1; min-width: 60px; background: #1a1a2e;
    border-radius: 2px; overflow: hidden;
}
.progress-fill { height: 100%; background: linear-gradient(90deg,#2563eb,#10b981); transition: width 0.3s; }
.round-counter { font-size: 0.72rem; color: #4a5568; white-space: nowrap; }

/* ── Arena ── */
.arena {
    flex: 1; display: grid; gap: 8px; padding: 8px;
    overflow-y: auto; align-content: start; min-height: 0;
}
.panel {
    display: flex; flex-direction: column;
    background: var(--hbg, #111); border: 1px solid var(--hbd, #2a2a40);
    border-radius: 12px; overflow: hidden;
    transition: box-shadow 0.25s; min-height: 180px;
}
.panel.speaking {
    box-shadow: 0 0 0 2px var(--ha), 0 4px 24px color-mix(in srgb, var(--ha) 25%, transparent);
}
.panel-header {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 13px; background: var(--hhdr, var(--hbg));
    border-bottom: 1px solid var(--hbd); flex-shrink: 0;
}
.hat-icon   { font-size: 1.3rem; flex-shrink: 0; }
.panel-label { font-size: 0.82rem; font-weight: 700; color: var(--hc); }
.panel-desc  { font-size: 0.6rem; color: var(--ha); }
.panel-body  { flex: 1; overflow-y: auto; padding: 8px; }
.speaking-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--ha, #888); margin-left: auto;
    opacity: 0; transition: opacity 0.2s;
}
.panel.speaking .speaking-dot { opacity: 1; animation: pulse-dot 1s infinite; }
@keyframes pulse-dot {
    0%,100% { opacity: 1; transform: scale(1); }
    50%      { opacity: 0.4; transform: scale(0.7); }
}
.placeholder { color: #3a3a5a; font-size: 0.78rem; font-style: italic; padding: 8px; text-align: center; }

/* ── Turn cards ── */
.turn-card {
    background: color-mix(in srgb, var(--ha, #4a8adc) 7%, #0d0d1a);
    border: 1px solid var(--hbd, #2a2a40); border-radius: 8px;
    padding: 9px 11px; margin-bottom: 6px; font-size: 0.82rem;
    line-height: 1.55; color: #c8c8d8;
}
.turn-card.active { border-color: var(--ha, #4a8adc); }
.card-header { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.round-tag {
    font-size: 0.6rem; font-weight: 800; color: var(--ha, #4a8adc);
    background: color-mix(in srgb, var(--ha, #4a8adc) 15%, transparent);
    padding: 1px 6px; border-radius: 5px;
}
.card-body a { color: var(--ha, #4a8adc); }
.card-waiting { display: flex; align-items: center; gap: 4px; color: #4a4a6a; font-size: 0.78rem; font-style: italic; }
.dot-anim { display: flex; gap: 2px; }
.dot-anim span { animation: bounce 1.2s infinite; font-style: normal; }
.dot-anim span:nth-child(2) { animation-delay: 0.2s; }
.dot-anim span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce { 0%,80%,100%{transform:translateY(0)} 40%{transform:translateY(-4px)} }
.error-msg { color: #f87171; font-size: 0.78rem; padding: 6px 10px; }
.round-sep {
    text-align: center; font-size: 0.6rem; color: #3a3a5a; letter-spacing: 2px;
    padding: 6px 0; text-transform: uppercase;
}

/* ── Verdict overlay ── */
.verdict-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: flex; align-items: center; justify-content: center; z-index: 100;
}
.verdict-modal {
    background: #111124; border: 1px solid #2a2a4a; border-radius: 16px;
    width: min(680px, 95vw); max-height: 88vh; display: flex; flex-direction: column;
    box-shadow: 0 24px 80px rgba(0,0,0,0.7);
}
.verdict-header {
    padding: 18px 22px 12px; border-bottom: 1px solid #1a1a3a; flex-shrink: 0;
}
.verdict-title { font-size: 0.65rem; font-weight: 800; letter-spacing: 2px; color: #4a5568; text-transform: uppercase; margin-bottom: 10px; }
.mvp-card {
    display: flex; align-items: center; gap: 14px; padding: 12px 16px;
    border-radius: 10px; background: var(--hbg, #0d0d1a); border: 2px solid var(--ha, #888);
    transition: all 0.3s;
}
.mvp-card.pending { background: #0d0d1a; border-color: #2a2a40; }
.mvp-emoji-large { font-size: 2rem; }
.mvp-name   { font-size: 1rem; font-weight: 800; color: var(--hc, #e0e0e0); }
.mvp-desc   { font-size: 0.68rem; color: var(--ha, #888); }
.mvp-crown  { font-size: 0.65rem; font-weight: 800; letter-spacing: 1px; color: #fbbf24; text-transform: uppercase; margin-left: auto; }
.verdict-body { flex: 1; overflow-y: auto; padding: 16px 22px; font-size: 0.85rem; line-height: 1.65; color: #c0c0d8; }
.verdict-body a { color: #60a5fa; }
.verdict-body p  { margin-bottom: 1em; }
.verdict-body h1, .verdict-body h2, .verdict-body h3 { margin-top: 1.2em; margin-bottom: 0.5em; color: #e0e0e0; }
.verdict-body ul, .verdict-body ol { margin: 0.5em 0 1em 1.4em; }
.verdict-body li { margin-bottom: 0.4em; }
.verdict-body strong { color: #e8e8f0; }
.verdict-footer { padding: 12px 22px; border-top: 1px solid #1a1a3a; display: flex; gap: 8px; flex-shrink: 0; flex-wrap: wrap; }

/* ── Email modal ── */
.email-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: flex; align-items: center; justify-content: center; z-index: 200;
}
.email-modal {
    background: #111124; border: 1px solid #2a2a4a; border-radius: 14px;
    width: min(420px, 95vw); box-shadow: 0 20px 60px rgba(0,0,0,0.6);
}
.email-hdr  { padding: 16px 20px 8px; border-bottom: 1px solid #1a1a3a; }
.email-title { font-size: 0.9rem; font-weight: 700; color: #e0e0e0; }
.email-desc  { font-size: 0.72rem; color: #555; margin-top: 3px; }
.email-body  { padding: 14px 20px; display: flex; flex-direction: column; gap: 10px; }
.email-label { font-size: 0.72rem; color: #888; margin-bottom: -4px; }
.email-input-field {
    padding: 8px 12px; border-radius: 8px; border: 1px solid #2a2a40;
    background: #0d0d1a; color: #e0e0e0; font-size: 0.85rem; width: 100%; outline: none;
}
.email-input-field:focus { border-color: #4a8adc; }
.email-remember { display: flex; align-items: center; gap: 8px; font-size: 0.75rem; color: #888; cursor: pointer; }
.email-status { font-size: 0.75rem; min-height: 1.2em; }
.email-status.ok  { color: #4ade80; }
.email-status.err { color: #f87171; }
.email-footer { padding: 10px 20px; border-top: 1px solid #1a1a3a; display: flex; justify-content: flex-end; gap: 8px; }

/* ── History modal ── */
.history-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: flex; align-items: center; justify-content: center; z-index: 200;
}
.history-modal {
    background: #111124; border: 1px solid #2a2a4a; border-radius: 14px;
    width: min(620px, 95vw); max-height: 85vh; display: flex; flex-direction: column;
    box-shadow: 0 20px 60px rgba(0,0,0,0.6);
}
.history-hdr {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px; border-bottom: 1px solid #1a1a3a; flex-shrink: 0;
}
.history-title { font-size: 0.9rem; font-weight: 700; color: #e0e0e0; }
.history-body  { flex: 1; overflow-y: auto; padding: 12px 20px; }
.history-empty { color: #3a3a5a; font-size: 0.82rem; font-style: italic; text-align: center; padding: 20px; }
.hist-entry {
    background: #0d0d1a; border: 1px solid #1a1a2e; border-radius: 10px;
    margin-bottom: 10px; overflow: hidden;
}
.hist-header {
    display: flex; align-items: flex-start; justify-content: space-between;
    padding: 10px 14px; gap: 10px; cursor: pointer;
    transition: background 0.15s;
}
.hist-header:hover { background: #111128; }
.hist-q    { font-size: 0.82rem; color: #c0c0d8; font-weight: 600; flex: 1; }
.hist-meta { font-size: 0.65rem; color: #555; flex-shrink: 0; text-align: right; }
.hist-mvp  { font-size: 0.68rem; font-weight: 700; padding: 1px 8px; border-radius: 6px; margin-top: 3px; }
.hist-body { padding: 0 14px 12px; display: none; }
.hist-body.open { display: block; }
.hist-verdict { font-size: 0.78rem; color: #a0a0b8; line-height: 1.55; }
.hist-actions { display: flex; gap: 6px; margin-top: 8px; }
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center;gap:13px;">
    <a href="https://www.bgadconsulting.com" target="_blank" rel="noopener noreferrer">
      <img src="/logo" alt="BGAD Consulting" class="brand-logo" onerror="this.style.display='none'" />
    </a>
    <div>
      <div class="logo">Six Thinking Hats Arena</div>
      <div class="logo-sub">Edward de Bono&rsquo;s framework &bull; AI-powered perspectives &bull; structured synthesis</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:12px;">
    <span style="font-size:0.6rem;color:#333;white-space:nowrap">Six Thinking Hats is &copy; The de Bono Group</span>
    <a href="https://www.bgadconsulting.com" target="_blank" rel="noopener noreferrer" class="brand-link">&copy; 2026 BGAD Consulting</a>
    <button class="btn btn-hist hidden" id="historyBtn" onclick="showHistory()">History</button>
    <div class="model-badge">claude-opus-4-6</div>
  </div>
</header>

<!-- Hat selector -->
<div class="hat-selector" id="hatSelector">
  <span style="font-size:0.65rem;color:#555;align-self:center;white-space:nowrap">Click to toggle hats on / off:</span>
  <div class="hat-toggle on" data-hat="white" onclick="toggleHat('white')">
    <span class="ht-emoji">⬜</span>
    <span class="ht-name">White</span>
    <span class="ht-desc">Facts</span>
  </div>
  <div class="hat-toggle on" data-hat="red" onclick="toggleHat('red')">
    <span class="ht-emoji">🔴</span>
    <span class="ht-name">Red</span>
    <span class="ht-desc">Emotions</span>
  </div>
  <div class="hat-toggle on" data-hat="yellow" onclick="toggleHat('yellow')">
    <span class="ht-emoji">🟡</span>
    <span class="ht-name">Yellow</span>
    <span class="ht-desc">Optimism</span>
  </div>
  <div class="hat-toggle on" data-hat="black" onclick="toggleHat('black')">
    <span class="ht-emoji">⬛</span>
    <span class="ht-name">Black</span>
    <span class="ht-desc">Risks</span>
  </div>
  <div class="hat-toggle on" data-hat="green" onclick="toggleHat('green')">
    <span class="ht-emoji">🟢</span>
    <span class="ht-name">Green</span>
    <span class="ht-desc">Ideas</span>
  </div>
  <div class="hat-toggle on" data-hat="blue" onclick="toggleHat('blue')">
    <span class="ht-emoji">🔵</span>
    <span class="ht-name">Blue</span>
    <span class="ht-desc">Overview</span>
  </div>
  <div class="cross-toggle on" id="crossToggle" onclick="toggleCross()">
    🔗 Hats listen to each other
    <span class="cross-pill" id="crossPill">ON</span>
  </div>
  <div class="cross-toggle" id="chairmanToggle" onclick="toggleChairman()">
    👔 Chairman mode
    <span class="cross-pill" id="chairmanPill">OFF</span>
  </div>
</div>

<!-- Input bar -->
<div class="input-bar">
  <div class="input-row">
    <input type="text" id="question"
           placeholder="Enter a topic or question — e.g. 'Should we expand into the Asian market?'" />
    <button class="btn btn-primary" id="startBtn"    onclick="startSession(false)">Start</button>
    <button class="btn btn-reset hidden" id="restartBtn" onclick="restartSession()">Restart</button>
    <button class="btn btn-rapid"  id="rapidBtn"   onclick="startSession(true)">Auto</button>
    <input  type="number" id="rapidRoundsInput" class="rapid-rounds-input" value="3" min="1" max="20" title="Rounds for Auto mode" />
    <label  class="rapid-rounds-label" for="rapidRoundsInput">rounds</label>
  </div>
</div>

<!-- Chairman picker bar -->
<div class="chairman-bar hidden" id="chairmanBar">
  <span class="chairman-label">👔 Pick the next hat:</span>
  <div class="chairman-hats" id="chairmanHats"></div>
  <button class="btn btn-judge" style="margin-left:auto" onclick="stopAndSynthesize()">Synthesise</button>
</div>

<!-- Controls bar -->
<div class="controls-bar hidden" id="controlsBar">
  <div class="ctrl-question" id="ctrlQuestion"></div>
  <span class="rapid-badge hidden" id="rapidBadge">Auto</span>
  <div class="ctrl-status idle" id="ctrlStatus">Ready</div>
  <div class="progress-wrap"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  <div class="round-counter" id="roundCounter">Round 0</div>
  <button class="btn btn-next  hidden" id="nextBtn"  onclick="nextRound()">Next Round</button>
  <button class="btn btn-judge hidden" id="judgeBtn" onclick="stopAndSynthesize()">Synthesise</button>
  <button class="btn btn-reset hidden" id="resetBtn" onclick="resetSession()">New Session</button>
</div>

<!-- Arena -->
<div class="arena" id="arena"></div>

<!-- Verdict overlay -->
<div class="verdict-overlay hidden" id="verdictOverlay">
  <div class="verdict-modal">
    <div class="verdict-header">
      <div class="verdict-title">Session Synthesis</div>
      <div class="mvp-card pending" id="mvpCard" data-hat="">
        <span class="mvp-emoji-large" id="mvpEmoji">🤔</span>
        <div>
          <div class="mvp-name" id="mvpName">Deliberating…</div>
          <div class="mvp-desc" id="mvpDesc"></div>
        </div>
        <div class="mvp-crown hidden" id="mvpCrown">Most Valuable Hat</div>
      </div>
    </div>
    <div class="verdict-body" id="verdictBody">
      <div class="card-waiting">Analysing the session
        <div class="dot-anim"><span>.</span><span>.</span><span>.</span></div>
      </div>
    </div>
    <div class="verdict-footer">
      <button class="btn btn-rejudge" onclick="reJudge()">Re-Synthesise</button>
      <button class="btn btn-email"   onclick="showEmailModal(-1)">Email</button>
      <button class="btn btn-pdf"     onclick="showPdfModal(-1)">PDF / Drive</button>
      <button class="btn btn-hist"    onclick="showHistory()">History</button>
      <button class="btn btn-reset"   onclick="resetSession()">New Session</button>
    </div>
  </div>
</div>

<!-- PDF / Drive modal -->
<div class="pdf-overlay hidden" id="pdfOverlay">
  <div class="pdf-modal">
    <div class="pdf-hdr">
      <div class="pdf-title">PDF &amp; Google Drive</div>
      <div class="pdf-desc" id="pdfDesc"></div>
    </div>
    <div class="pdf-body">
      <!-- Download PDF -->
      <div class="pdf-option">
        <div class="pdf-opt-info">
          <div class="pdf-opt-name">⬇ Download PDF</div>
          <div class="pdf-opt-desc">Save the session as a formatted PDF file</div>
        </div>
        <button class="btn btn-pdf" id="pdfDownloadBtn" onclick="downloadPdf()" style="font-size:0.75rem;padding:6px 14px">Download</button>
      </div>
      <!-- Upload to Drive -->
      <div class="pdf-option">
        <div class="pdf-opt-info">
          <div class="pdf-opt-name">&#9729; Upload to Google Drive</div>
          <div class="pdf-opt-desc">Generate PDF and save directly to your Drive</div>
          <div class="drive-auth-row" id="driveAuthRow">
            <div class="drive-auth-dot" id="driveAuthDot" style="background:#555"></div>
            <span id="driveAuthLabel">Checking…</span>
            <a href="#" id="driveAuthLink" onclick="connectDrive();return false;"
               style="color:#60a5fa;font-size:0.68rem;display:none">Connect Google Drive</a>
          </div>
        </div>
        <button class="btn btn-pdf" id="pdfDriveBtn" onclick="uploadToDrive()" style="font-size:0.75rem;padding:6px 14px" disabled>Upload</button>
      </div>
      <div class="pdf-status" id="pdfStatus"></div>
    </div>
    <div class="pdf-footer">
      <button class="btn btn-reset" onclick="hidePdfModal()">Close</button>
    </div>
  </div>
</div>

<!-- Email modal -->
<div class="email-overlay hidden" id="emailOverlay">
  <div class="email-modal">
    <div class="email-hdr">
      <div class="email-title">Send Session by Email</div>
      <div class="email-desc" id="emailDesc"></div>
    </div>
    <div class="email-body">
      <div class="email-label">From (Gmail address)</div>
      <input class="email-input-field" type="email" id="emailFrom" placeholder="you@gmail.com" />
      <div class="email-label">App Password <span style="font-size:0.65rem;color:#555">(myaccount.google.com → Security → App Passwords)</span></div>
      <input class="email-input-field" type="password" id="emailAppPass" placeholder="xxxx xxxx xxxx xxxx" />
      <div class="email-label">Send to</div>
      <input class="email-input-field" type="email" id="emailTo" placeholder="recipient@example.com"
             onkeypress="if(event.key==='Enter') doSendEmail()" />
      <label class="email-remember">
        <input type="checkbox" id="emailRemember" checked />
        Remember sender credentials
      </label>
      <div class="email-status" id="emailStatus"></div>
    </div>
    <div class="email-footer">
      <button class="btn btn-reset" onclick="hideEmailModal()">Cancel</button>
      <button class="btn btn-email" id="emailSendBtn" onclick="doSendEmail()">Send</button>
    </div>
  </div>
</div>

<!-- History modal -->
<div class="history-overlay hidden" id="historyOverlay">
  <div class="history-modal">
    <div class="history-hdr">
      <div class="history-title">Session History</div>
      <button class="btn btn-reset" style="padding:5px 14px;font-size:0.75rem" onclick="hideHistory()">Close</button>
    </div>
    <div class="history-body" id="historyBody"></div>
  </div>
</div>

<script>
// ── marked config ────────────────────────────────────────────────────────────
const renderer = new marked.Renderer();
const _link = renderer.link.bind(renderer);
renderer.link = (href, title, text) =>
    _link(href, title, text).replace('<a ', '<a target="_blank" rel="noopener noreferrer" ');
marked.setOptions({ renderer, breaks: true, gfm: true });

// ── Hat definitions ──────────────────────────────────────────────────────────
const HAT_ORDER = ['white','red','yellow','black','green','blue'];
const HAT_DEFS  = {
    white:  { label:'White Hat',  desc:'Facts & Data',          emoji:'⬜', color:'#e5e7eb', accent:'#9ca3af' },
    red:    { label:'Red Hat',    desc:'Emotions & Intuition',  emoji:'🔴', color:'#fca5a5', accent:'#ef4444' },
    yellow: { label:'Yellow Hat', desc:'Optimism & Benefits',   emoji:'🟡', color:'#fde68a', accent:'#f59e0b' },
    black:  { label:'Black Hat',  desc:'Caution & Risks',       emoji:'⬛', color:'#d1d5db', accent:'#6b7280' },
    green:  { label:'Green Hat',  desc:'Creativity & Ideas',    emoji:'🟢', color:'#6ee7b7', accent:'#10b981' },
    blue:   { label:'Blue Hat',   desc:'Process & Overview',    emoji:'🔵', color:'#93c5fd', accent:'#3b82f6' },
};

// ── State ────────────────────────────────────────────────────────────────────
let question        = '';
let crossPollinate  = true;
let activeHats      = [...HAT_ORDER];   // all on by default
let currentRound    = 0;
let roundInProgress = false;
let judging         = false;
let rapidMode       = false;
let rapidRounds     = 3;
let chairmanMode    = false;
let abortCtrl       = null;
let judgeVer        = 0;
let transcript      = [];     // { round, hat, text }
let currentMvpHat   = '';
let currentVerdictText = '';
let emailTargetIdx  = -1;
let sessionHistory  = [];     // { question, rounds, activeHats, mvpHat, verdictText, transcript }

// per-hat streaming state
let currentHat      = null;
let currentCard     = null;
let currentText     = '';

// ── Hat selector ─────────────────────────────────────────────────────────────
function toggleHat(hat_id) {
    if (judging || roundInProgress) return;
    const el = document.querySelector(`.hat-toggle[data-hat="${hat_id}"]`);
    const on = el.classList.toggle('on');
    if (on) {
        if (!activeHats.includes(hat_id)) activeHats.push(hat_id);
        // keep HAT_ORDER order
        activeHats.sort((a,b) => HAT_ORDER.indexOf(a) - HAT_ORDER.indexOf(b));
    } else {
        activeHats = activeHats.filter(h => h !== hat_id);
    }
}

function lockHatSelector(locked) {
    document.querySelectorAll('.hat-toggle').forEach(el =>
        el.classList.toggle('disabled', locked));
    ['crossToggle', 'chairmanToggle'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.pointerEvents = locked ? 'none' : '';
    });
}

function toggleCross() {
    if (document.getElementById('crossToggle').style.pointerEvents === 'none') return;
    crossPollinate = !crossPollinate;
    document.getElementById('crossToggle').classList.toggle('on', crossPollinate);
    document.getElementById('crossPill').textContent = crossPollinate ? 'ON' : 'OFF';
}

function toggleChairman() {
    if (roundInProgress || judging) return;
    chairmanMode = !chairmanMode;
    document.getElementById('chairmanToggle').classList.toggle('on', chairmanMode);
    document.getElementById('chairmanPill').textContent = chairmanMode ? 'ON' : 'OFF';
    // Disable / re-enable Auto mode controls
    ['rapidBtn', 'rapidRoundsInput'].forEach(id => {
        document.getElementById(id).classList.toggle('rapid-disabled', chairmanMode);
    });
    document.querySelector('.rapid-rounds-label').classList.toggle('rapid-disabled', chairmanMode);
    // Disable / re-enable hat toggles
    document.querySelectorAll('.hat-toggle').forEach(el =>
        el.classList.toggle('disabled', chairmanMode));
}

// ── Chairman mode helpers ────────────────────────────────────────────────────
function showChairmanPicker() {
    const hatsDiv = document.getElementById('chairmanHats');
    hatsDiv.innerHTML = '';
    activeHats.forEach(hat_id => {
        const d   = HAT_DEFS[hat_id];
        const btn = document.createElement('div');
        btn.className = 'chairman-hat-btn';
        btn.setAttribute('data-hat', hat_id);
        btn.innerHTML  = d.emoji + ' ' + d.label;
        btn.onclick    = () => callChairmanHat(hat_id);
        hatsDiv.appendChild(btn);
    });
    document.getElementById('chairmanBar').classList.remove('hidden');
    setStatus('wait', '👔 Chairman — pick the next hat');
}

function hideChairmanPicker() {
    document.getElementById('chairmanBar').classList.add('hidden');
}

function callChairmanHat(hat_id) {
    hideChairmanPicker();
    nextRound([hat_id]);
}

// ── Arena builder ────────────────────────────────────────────────────────────
function buildArena() {
    const arena = document.getElementById('arena');
    arena.innerHTML = '';
    const n    = activeHats.length;
    const cols = n <= 3 ? n : n === 4 ? 2 : 3;
    arena.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;

    activeHats.forEach(hat_id => {
        const d = HAT_DEFS[hat_id];
        const panel = document.createElement('div');
        panel.className = 'panel';
        panel.id        = 'panel-' + hat_id;
        panel.dataset.hat = hat_id;
        panel.innerHTML =
            '<div class="panel-header">'
          + '<span class="hat-icon">' + d.emoji + '</span>'
          + '<div><div class="panel-label">' + d.label + '</div>'
          + '<div class="panel-desc">' + d.desc + '</div></div>'
          + '<div class="speaking-dot"></div>'
          + '</div>'
          + '<div class="panel-body" id="body-' + hat_id + '">'
          + '<div class="placeholder">Points will appear here…</div>'
          + '</div>';
        arena.appendChild(panel);
    });
}

// ── Session flow ─────────────────────────────────────────────────────────────
function startSession(rapid) {
    question = document.getElementById('question').value.trim();
    if (!question) { alert('Please enter a topic or question!'); return; }
    if (activeHats.length < 2) { alert('Please select at least 2 hats.'); return; }

    rapidMode       = !!rapid;
    rapidRounds     = rapidMode
        ? Math.max(1, parseInt(document.getElementById('rapidRoundsInput').value) || 3)
        : 3;
    roundInProgress = false;
    currentRound    = 0;
    transcript      = [];
    judging         = false;
    currentMvpHat   = '';
    currentVerdictText = '';

    buildArena();
    lockHatSelector(true);

    document.getElementById('verdictOverlay').classList.add('hidden');
    document.getElementById('ctrlQuestion').innerHTML =
        'Topic: <strong>' + escHtml(question) + '</strong>';
    document.getElementById('controlsBar').classList.remove('hidden');

    setBtn('startBtn', false, 'Running…');
    setBtn('rapidBtn', false, 'Auto');
    show('restartBtn');
    hide('nextBtn'); hide('resetBtn');
    document.getElementById('nextBtn').textContent = 'Next Round';

    if (chairmanMode) {
        document.getElementById('rapidBadge').textContent = '👔 Chairman';
        show('rapidBadge');
    } else if (rapidMode) {
        document.getElementById('rapidBadge').textContent =
            'Auto \u2022 ' + rapidRounds + ' rounds';
        show('rapidBadge');
    } else {
        hide('rapidBadge');
    }

    show('judgeBtn');
    setStatus('idle', 'Starting…');

    if (chairmanMode) {
        roundInProgress = false;
        showChairmanPicker();
    } else {
        nextRound();
    }
}

function nextRound(hatsOverride) {
    if (judging || roundInProgress) return;
    roundInProgress = true;
    hide('nextBtn'); hide('resetBtn');
    show('judgeBtn');

    currentRound++;
    currentHat  = null;
    currentCard = null;
    currentText = '';

    const hatsForRound = hatsOverride || activeHats;

    // Separators: in chairman mode only add to the speaking hat's panel;
    // in normal mode add to all panels after round 1.
    if (chairmanMode) {
        hatsForRound.forEach(hat_id => {
            const body = document.getElementById('body-' + hat_id);
            if (body) {
                const sep = document.createElement('div');
                sep.className = 'round-sep';
                sep.textContent = '— Turn ' + currentRound + ' —';
                body.appendChild(sep);
            }
        });
    } else if (currentRound > 1) {
        activeHats.forEach(hat_id => {
            const body = document.getElementById('body-' + hat_id);
            if (body) {
                const sep = document.createElement('div');
                sep.className = 'round-sep';
                sep.textContent = '— Round ' + currentRound + ' —';
                body.appendChild(sep);
            }
        });
    }

    setRoundCounter();
    setStatus('idle', chairmanMode
        ? HAT_DEFS[hatsForRound[0]].label + ' thinking…'
        : 'Round ' + currentRound + ' — starting…');
    setProgress(0, hatsForRound.length);

    if (abortCtrl) abortCtrl.abort();
    abortCtrl = new AbortController();

    fetch('/think', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, round: currentRound, hats: hatsForRound, transcript, cross_pollinate: crossPollinate }),
        signal: abortCtrl.signal,
    }).then(resp => {
        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        function read() {
            reader.read().then(({ done, value }) => {
                if (done) return;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    let d;
                    try { d = JSON.parse(line.slice(6)); } catch { continue; }
                    handleThinkEvent(d);
                }
                read();
            }).catch(err => {
                if (err.name !== 'AbortError' && !judging) {
                    setStatus('idle', 'Connection error');
                    roundInProgress = false;
                    show('resetBtn');
                }
            });
        }
        read();
    }).catch(err => {
        if (err.name !== 'AbortError' && !judging) {
            setStatus('idle', 'Connection error');
            roundInProgress = false;
            show('resetBtn');
        }
    });
}

function stopAndSynthesize() {
    if (judging) return;
    judging         = true;
    roundInProgress = false;
    finaliseCurrentCard();
    cleanup();
    hideChairmanPicker();
    hide('judgeBtn'); hide('nextBtn');
    show('resetBtn');
    setBtn('startBtn', true, 'Start');
    hide('restartBtn');
    setBtn('rapidBtn', true, 'Auto');

    if (transcript.length === 0) {
        alert('No points yet — let at least one round complete first.');
        judging = false;
        show('judgeBtn');
        setBtn('startBtn', false, 'Running…');
        setBtn('rapidBtn', false, 'Auto');
        return;
    }
    runJudge();
}

function resetSession() {
    judging         = false;
    rapidMode       = false;
    chairmanMode    = false;
    roundInProgress = false;
    crossPollinate  = true;
    document.getElementById('crossToggle').classList.add('on');
    document.getElementById('crossPill').textContent = 'ON';
    document.getElementById('chairmanToggle').classList.remove('on');
    document.getElementById('chairmanPill').textContent = 'OFF';
    ['rapidBtn', 'rapidRoundsInput'].forEach(id => {
        document.getElementById(id).classList.remove('rapid-disabled');
    });
    document.querySelector('.rapid-rounds-label').classList.remove('rapid-disabled');
    hideChairmanPicker();
    cleanup();
    currentRound  = 0;
    transcript    = [];
    currentMvpHat = '';
    currentVerdictText = '';

    document.getElementById('verdictOverlay').classList.add('hidden');
    document.getElementById('controlsBar').classList.add('hidden');
    document.getElementById('arena').innerHTML = '';
    setProgress(0, 1);
    setStatus('idle', 'Ready');
    hide('judgeBtn'); hide('nextBtn'); hide('resetBtn'); hide('rapidBadge');
    setBtn('startBtn', true, 'Start');
    hide('restartBtn');
    setBtn('rapidBtn', true, 'Auto');
    lockHatSelector(false);
    document.getElementById('question').focus();
}

function restartSession() {
    const savedQuestion = document.getElementById('question').value;
    resetSession();
    document.getElementById('question').value = savedQuestion;
    document.getElementById('question').focus();
    document.getElementById('question').select();
}

function cleanup() {
    if (abortCtrl) { abortCtrl.abort(); abortCtrl = null; }
}

// ── Think events ─────────────────────────────────────────────────────────────
let hatsDoneThisRound = 0;

function handleThinkEvent(data) {
    if (judging) return;
    switch (data.type) {

        case 'turn_start': {
            currentHat  = data.hat;
            currentText = '';
            const panel = document.getElementById('panel-' + data.hat);
            const body  = document.getElementById('body-' + data.hat);
            if (!panel || !body) break;

            // Remove placeholder
            const ph = body.querySelector('.placeholder');
            if (ph) ph.remove();

            // Set panel as speaking
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('speaking'));
            panel.classList.add('speaking');

            const d = HAT_DEFS[data.hat];
            setStatus(data.hat, d.label + ' thinking…');

            const card = document.createElement('div');
            card.className = 'turn-card active';
            card.innerHTML =
                '<div class="card-header">'
              + '<span class="round-tag">R' + data.round + '</span>'
              + '</div>'
              + '<div class="card-body">'
              + '<div class="card-waiting">Thinking'
              + '<div class="dot-anim"><span>.</span><span>.</span><span>.</span></div>'
              + '</div></div>';
            body.appendChild(card);
            body.scrollTop = body.scrollHeight;
            currentCard = card;

            // Update progress
            hatsDoneThisRound = activeHats.indexOf(data.hat);
            setProgress(hatsDoneThisRound, activeHats.length);
            break;
        }

        case 'chunk':
            if (!currentCard || judging) break;
            currentText += data.text;
            renderCard(currentCard, currentText, true);
            const body2 = document.getElementById('body-' + data.hat);
            if (body2) body2.scrollTop = body2.scrollHeight;
            break;

        case 'turn_end':
            if (currentCard) {
                renderCard(currentCard, currentText, false);
                currentCard.classList.remove('active');
                transcript.push({ round: data.round, hat: data.hat, text: currentText });
                currentCard = null;
                currentText = '';
            }
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('speaking'));
            hatsDoneThisRound++;
            setProgress(hatsDoneThisRound, activeHats.length);
            break;

        case 'round_done':
            roundInProgress = false;
            cleanup();
            setProgress(activeHats.length, activeHats.length);
            if (chairmanMode) {
                setStatus('done', 'Done — pick next hat');
                showChairmanPicker();
            } else if (rapidMode) {
                if (currentRound < rapidRounds) {
                    setStatus('idle', 'Round ' + currentRound + ' / ' + rapidRounds + ' done — next starting…');
                    setTimeout(nextRound, 1200);
                } else {
                    setStatus('done', 'All ' + rapidRounds + ' rounds done — synthesising…');
                    setTimeout(stopAndSynthesize, 900);
                }
            } else {
                setStatus('done', 'Round ' + currentRound + ' done');
                document.getElementById('nextBtn').textContent =
                    'Next Round (' + (currentRound + 1) + ')';
                show('nextBtn');
            }
            break;

        case 'error':
            activeHats.forEach(hat_id => {
                const b = document.getElementById('body-' + hat_id);
                if (b) b.innerHTML += '<div class="error-msg">Error: ' + escHtml(data.message) + '</div>';
            });
            roundInProgress = false;
            cleanup();
            hide('judgeBtn'); show('resetBtn');
            setStatus('idle', 'Error');
            break;
    }
}

// ── Judge ────────────────────────────────────────────────────────────────────
async function runJudge() {
    const myVer = ++judgeVer;

    setStatus('done', 'Synthesising…');
    document.getElementById('verdictOverlay').classList.remove('hidden');

    // Reset MVP card to pending
    const mvpCard = document.getElementById('mvpCard');
    mvpCard.className = 'mvp-card pending';
    mvpCard.dataset.hat = '';
    document.getElementById('mvpEmoji').textContent = '🤔';
    document.getElementById('mvpName').textContent  = 'Deliberating…';
    document.getElementById('mvpDesc').textContent  = '';
    document.getElementById('mvpCrown').classList.add('hidden');
    // Show streaming placeholder — verdict body will fill as chunks arrive
    document.getElementById('verdictBody').innerHTML =
        '<div class="card-waiting">Synthesising'
      + '<div class="dot-anim"><span>.</span><span>.</span><span>.</span></div></div>';

    let judgeText = '';
    let sseBuffer = '';
    let firstChunk = true;

    try {
        const resp = await fetch('/judge_hats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question, transcript, hats: activeHats }),
        });

        if (myVer !== judgeVer) return;

        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();
            if (myVer !== judgeVer) { reader.cancel(); return; }
            if (done) break;

            sseBuffer += decoder.decode(value, { stream: true });
            const lines = sseBuffer.split('\n');
            sseBuffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                let d;
                try { d = JSON.parse(line.slice(6)); } catch { continue; }

                if (d.type === 'chunk') {
                    if (firstChunk) {
                        // Clear placeholder on first real content
                        document.getElementById('verdictBody').innerHTML = '';
                        firstChunk = false;
                    }
                    judgeText += d.text;
                    document.getElementById('verdictBody').innerHTML =
                        marked.parse(judgeText) + '<span class="cursor">▍</span>';

                } else if (d.type === 'mvp') {
                    // MVP arrives after all chunks — update card and finalise text
                    currentMvpHat = d.hat;
                    const info = HAT_DEFS[d.hat] || {};
                    mvpCard.dataset.hat = d.hat || '';
                    mvpCard.className   = d.hat ? 'mvp-card' : 'mvp-card pending';
                    document.getElementById('mvpEmoji').textContent = info.emoji || '🎩';
                    document.getElementById('mvpName').textContent  = info.label || 'Session Complete';
                    document.getElementById('mvpDesc').textContent  = info.desc  || '';
                    if (d.hat) document.getElementById('mvpCrown').classList.remove('hidden');
                    // Use the clean display text (MVP line stripped by server)
                    const displayText = d.display || judgeText;
                    document.getElementById('verdictBody').innerHTML = marked.parse(displayText);
                    currentVerdictText = displayText;

                } else if (d.type === 'done') {
                    sessionHistory.push({
                        question, rounds: currentRound,
                        activeHats: [...activeHats],
                        mvpHat: currentMvpHat,
                        verdictText: currentVerdictText,
                        transcript: [...transcript],
                    });
                    show('historyBtn');

                } else if (d.type === 'error') {
                    document.getElementById('verdictBody').innerHTML =
                        '<div class="error-msg">Error: ' + escHtml(d.message) + '</div>';
                }
            }
        }
    } catch (err) {
        if (myVer !== judgeVer) return;
        document.getElementById('verdictBody').innerHTML =
            '<div class="error-msg">Error: ' + escHtml(err.message) + '</div>';
    }
}

function reJudge() { runJudge(); }

// ── PDF / Drive ───────────────────────────────────────────────────────────────
let pdfTargetIdx = -1;

function _pdfPayload(idx) {
    if (idx >= 0) {
        const e = sessionHistory[idx];
        return { question: e.question, rounds: e.rounds, hats: e.activeHats,
                 mvpHat: e.mvpHat, verdictText: e.verdictText, transcript: e.transcript };
    }
    return { question, rounds: currentRound, hats: activeHats,
             mvpHat: currentMvpHat, verdictText: currentVerdictText,
             transcript: [...transcript] };
}

async function showPdfModal(idx) {
    pdfTargetIdx = idx;
    const q = idx >= 0 ? sessionHistory[idx].question : question;
    document.getElementById('pdfDesc').textContent = q.length > 65 ? q.slice(0,62)+'…' : q;
    document.getElementById('pdfStatus').textContent = '';
    document.getElementById('pdfStatus').className = 'pdf-status';
    setBtn('pdfDownloadBtn', true, 'Download');
    document.getElementById('pdfOverlay').classList.remove('hidden');
    await refreshDriveStatus();
}

function hidePdfModal() {
    document.getElementById('pdfOverlay').classList.add('hidden');
}

async function refreshDriveStatus() {
    const dot   = document.getElementById('driveAuthDot');
    const label = document.getElementById('driveAuthLabel');
    const link  = document.getElementById('driveAuthLink');
    const btn   = document.getElementById('pdfDriveBtn');
    label.textContent = 'Checking…'; dot.style.background = '#555';
    try {
        const r = await fetch('/gdrive/status');
        const d = await r.json();
        if (!d.configured) {
            dot.style.background = '#f59e0b';
            label.textContent = 'Not configured — see setup guide below';
            link.style.display = 'none'; btn.disabled = true;
        } else if (d.authenticated) {
            dot.style.background = '#10b981';
            label.textContent = 'Connected to Google Drive';
            link.style.display = 'none'; btn.disabled = false;
        } else {
            dot.style.background = '#ef4444';
            label.textContent = 'Not connected — ';
            link.style.display = ''; btn.disabled = true;
        }
    } catch {
        label.textContent = 'Status check failed'; dot.style.background = '#555';
    }
}

function connectDrive() {
    const win = window.open('/gdrive/auth', '_blank', 'width=520,height=620');
    window.addEventListener('message', async function handler(e) {
        if (e.data === 'gdrive_ok') {
            window.removeEventListener('message', handler);
            if (win && !win.closed) win.close();
            await refreshDriveStatus();
        }
    });
}

async function downloadPdf() {
    const st = document.getElementById('pdfStatus');
    setBtn('pdfDownloadBtn', false, 'Generating…');
    st.textContent = ''; st.className = 'pdf-status';
    try {
        const resp = await fetch('/generate_pdf', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_pdfPayload(pdfTargetIdx)),
        });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            st.textContent = d.error || 'PDF generation failed.'; st.className = 'pdf-status err';
        } else {
            const blob = await resp.blob();
            const url  = URL.createObjectURL(blob);
            const a    = document.createElement('a');
            const cd   = resp.headers.get('Content-Disposition') || '';
            const m    = cd.match(/filename="(.+?)"/);
            a.href = url; a.download = m ? m[1] : 'session.pdf';
            document.body.appendChild(a); a.click();
            document.body.removeChild(a); URL.revokeObjectURL(url);
            st.textContent = '✓ PDF downloaded'; st.className = 'pdf-status ok';
        }
    } catch (err) {
        st.textContent = 'Error: ' + err.message; st.className = 'pdf-status err';
    }
    setBtn('pdfDownloadBtn', true, 'Download');
}

async function uploadToDrive() {
    const st = document.getElementById('pdfStatus');
    setBtn('pdfDriveBtn', false, 'Uploading…');
    st.textContent = 'Generating PDF…'; st.className = 'pdf-status inf';
    try {
        const resp = await fetch('/gdrive/upload', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_pdfPayload(pdfTargetIdx)),
        });
        const d = await resp.json();
        if (d.ok) {
            st.innerHTML = `✓ Uploaded: <a href="${d.link}" target="_blank" rel="noopener"
                style="color:#60a5fa">${d.name}</a>`;
            st.className = 'pdf-status ok';
        } else if (d.need_auth) {
            st.textContent = 'Not authenticated — connect Google Drive first.';
            st.className = 'pdf-status err';
            await refreshDriveStatus();
        } else {
            st.textContent = d.error || 'Upload failed.'; st.className = 'pdf-status err';
        }
    } catch (err) {
        st.textContent = 'Error: ' + err.message; st.className = 'pdf-status err';
    }
    setBtn('pdfDriveBtn', true, 'Upload');
}

// ── Email ────────────────────────────────────────────────────────────────────
function showEmailModal(idx) {
    emailTargetIdx = idx;
    const q = idx >= 0 ? sessionHistory[idx].question : question;
    document.getElementById('emailDesc').textContent =
        (q.length > 65 ? q.slice(0, 62) + '…' : q);
    document.getElementById('emailFrom').value    = localStorage.getItem('hats_smtp_user') || '';
    document.getElementById('emailAppPass').value = localStorage.getItem('hats_smtp_pass') || '';
    document.getElementById('emailTo').value      = '';
    const st = document.getElementById('emailStatus');
    st.textContent = ''; st.className = 'email-status';
    setBtn('emailSendBtn', true, 'Send');
    document.getElementById('emailOverlay').classList.remove('hidden');
    setTimeout(() => {
        const f = document.getElementById('emailFrom').value ? 'emailTo' : 'emailFrom';
        document.getElementById(f).focus();
    }, 60);
}

function hideEmailModal() {
    document.getElementById('emailOverlay').classList.add('hidden');
}

async function doSendEmail() {
    const fromEmail = document.getElementById('emailFrom').value.trim();
    const appPass   = document.getElementById('emailAppPass').value.trim();
    const toEmail   = document.getElementById('emailTo').value.trim();
    const remember  = document.getElementById('emailRemember').checked;
    const st        = document.getElementById('emailStatus');

    if (!fromEmail || !appPass || !toEmail) {
        st.textContent = 'Please fill in all three fields.';
        st.className = 'email-status err'; return;
    }

    if (remember) {
        localStorage.setItem('hats_smtp_user', fromEmail);
        localStorage.setItem('hats_smtp_pass', appPass);
    } else {
        localStorage.removeItem('hats_smtp_user');
        localStorage.removeItem('hats_smtp_pass');
    }

    let payload;
    if (emailTargetIdx >= 0) {
        const e = sessionHistory[emailTargetIdx];
        payload = { smtp_user: fromEmail, smtp_pass: appPass, to_email: toEmail,
                    question: e.question, rounds: e.rounds, hats: e.activeHats,
                    mvpHat: e.mvpHat, verdictText: e.verdictText, transcript: e.transcript };
    } else {
        payload = { smtp_user: fromEmail, smtp_pass: appPass, to_email: toEmail,
                    question, rounds: currentRound, hats: activeHats,
                    mvpHat: currentMvpHat, verdictText: currentVerdictText,
                    transcript: [...transcript] };
    }

    setBtn('emailSendBtn', false, 'Sending…');
    st.textContent = ''; st.className = 'email-status';
    try {
        const resp = await fetch('/send_email', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.ok) {
            st.textContent = '✓ Sent successfully!'; st.className = 'email-status ok';
            setBtn('emailSendBtn', true, 'Send');
            setTimeout(hideEmailModal, 1800);
        } else {
            st.textContent = data.error || 'Send failed.'; st.className = 'email-status err';
            setBtn('emailSendBtn', true, 'Send');
        }
    } catch (err) {
        st.textContent = 'Network error: ' + err.message; st.className = 'email-status err';
        setBtn('emailSendBtn', true, 'Send');
    }
}

// ── History ──────────────────────────────────────────────────────────────────
function showHistory() {
    const body = document.getElementById('historyBody');
    if (sessionHistory.length === 0) {
        body.innerHTML = '<div class="history-empty">No sessions yet.</div>';
    } else {
        const HAT_ACCENT = {
            white:'#9ca3af', red:'#ef4444', yellow:'#f59e0b',
            black:'#6b7280', green:'#10b981', blue:'#3b82f6',
        };
        body.innerHTML = sessionHistory.slice().reverse().map((e, ri) => {
            const idx   = sessionHistory.length - 1 - ri;
            const mvp   = HAT_DEFS[e.mvpHat] || {};
            const color = HAT_ACCENT[e.mvpHat] || '#888';
            const hats_icons = e.activeHats.map(h => HAT_DEFS[h]?.emoji || '').join(' ');
            return `<div class="hist-entry">
                <div class="hist-header" onclick="toggleHistEntry(${idx})">
                  <div>
                    <div class="hist-q">${escHtml(e.question)}</div>
                    <div style="font-size:0.65rem;color:#555;margin-top:3px">
                      ${hats_icons} &bull; ${e.rounds} round${e.rounds!==1?'s':''}
                    </div>
                    ${e.mvpHat ? `<div class="hist-mvp" style="color:${color};background:color-mix(in srgb,${color} 15%,transparent)">${mvp.emoji||''} ${mvp.label||''}  — Most Valuable Hat</div>` : ''}
                  </div>
                </div>
                <div class="hist-body" id="hb-${idx}">
                  <div class="hist-verdict">${marked.parse(e.verdictText || '')}</div>
                  <div class="hist-actions">
                    <button class="btn btn-email" style="font-size:0.72rem;padding:5px 12px"
                            onclick="showEmailModal(${idx});hideHistory()">Email</button>
                    <button class="btn btn-pdf" style="font-size:0.72rem;padding:5px 12px"
                            onclick="showPdfModal(${idx});hideHistory()">PDF / Drive</button>
                  </div>
                </div>
              </div>`;
        }).join('');
    }
    document.getElementById('historyOverlay').classList.remove('hidden');
}

function hideHistory() {
    document.getElementById('historyOverlay').classList.add('hidden');
}

function toggleHistEntry(idx) {
    const el = document.getElementById('hb-' + idx);
    if (el) el.classList.toggle('open');
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function finaliseCurrentCard() {
    if (currentCard) {
        renderCard(currentCard, currentText, false);
        currentCard.classList.remove('active');
        currentCard = null;
        currentText = '';
    }
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('speaking'));
}

function renderCard(card, text, streaming) {
    const body = card.querySelector('.card-body');
    if (!body) return;
    if (!text && streaming) return;
    const html = marked.parse(text || '');
    body.innerHTML = streaming ? html + '<span class="cursor">▍</span>' : html;
}

function setRoundCounter() {
    const label = chairmanMode
        ? 'Turn ' + currentRound
        : rapidMode
            ? 'Round ' + currentRound + ' / ' + rapidRounds
            : 'Round ' + currentRound;
    document.getElementById('roundCounter').textContent = label;
}

function setProgress(done, total) {
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    document.getElementById('progressFill').style.width = pct + '%';
}

function setStatus(cls, text) {
    const el = document.getElementById('ctrlStatus');
    el.className = 'ctrl-status ' + cls;
    el.textContent = text;
}

function setBtn(id, enabled, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = !enabled;
    if (text) el.textContent = text;
}

function show(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hide(id) { document.getElementById(id)?.classList.add('hidden'); }

function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                    .replace(/"/g,'&quot;');
}
</script>
</body>
</html>
"""

# ── Launch ────────────────────────────────────────────────────────────────────

def _launch_chrome(url):
    time.sleep(1.5)
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in chrome_paths:
        if os.path.exists(exe):
            webbrowser.register("chrome", None, webbrowser.BackgroundBrowser(exe))
            webbrowser.get("chrome").open(url)
            return
    print("[!] Chrome not found; opening with the default browser instead.")
    webbrowser.open(url)


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n[!] WARNING: ANTHROPIC_API_KEY environment variable is not set.")
        print("    Set it with:  set ANTHROPIC_API_KEY=your-key-here\n")
    port = int(os.environ.get("PORT", 5001))
    url  = f"http://localhost:{port}"
    print(f"[*] Starting Six Thinking Hats Arena on {url}")
    if not os.environ.get("PORT"):
        threading.Thread(target=_launch_chrome, args=(url,), daemon=True).start()
    app.run(debug=False, port=port, threaded=True)
