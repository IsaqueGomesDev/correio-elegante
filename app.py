from flask import (Flask, render_template, request, jsonify,
                   Response, redirect, url_for, session, send_from_directory)
from datetime import datetime, timedelta
import json, os, csv, io, sqlite3, base64, secrets

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DB_FILE     = os.environ.get("DB_PATH", "correio.db")
ADMIN_USER  = os.environ.get("ADMIN_USER",  "manoelder")
ADMIN_PASS  = os.environ.get("ADMIN_PASS",  "reidosratos")
PIX_CHAVE   = os.environ.get("PIX_CHAVE",   "11999999999")
PIX_NOME    = os.environ.get("PIX_NOME",    "Terceirão 2026")

PACOTES = {
    "carta":             {"nome": "Carta Normal",          "preco": 2.00,  "emoji": "💌"},
    "carta_pirulito":    {"nome": "Carta + Pirulito",      "preco": 3.00,  "emoji": "🍭"},
    "carta_bombom":      {"nome": "Carta + Bombom",        "preco": 4.00,  "emoji": "🍫"},
    "carta_flor":        {"nome": "Carta + Flor",          "preco": 8.00,  "emoji": "🌹"},
    "carta_bombom_flor": {"nome": "Carta + Bombom e Flor", "preco": 10.00, "emoji": "🌹🍫"},
}

# ── DB ─────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                remetente       TEXT NOT NULL,
                destinatario    TEXT NOT NULL,
                pacote_id       TEXT NOT NULL,
                pacote_nome     TEXT NOT NULL,
                emoji           TEXT NOT NULL,
                preco           REAL NOT NULL,
                mensagem        TEXT NOT NULL,
                anonimo         INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'aguardando_pagamento',
                comprovante     TEXT,
                data_pedido     TEXT NOT NULL,
                data_pagamento  TEXT,
                finalizado      INTEGER NOT NULL DEFAULT 0,
                data_finalizado TEXT
            )
        """)
        # adiciona colunas novas em bancos antigos sem errar
        for col, dfn in [("finalizado","INTEGER NOT NULL DEFAULT 0"),
                         ("data_finalizado","TEXT")]:
            try:
                conn.execute(f"ALTER TABLE pedidos ADD COLUMN {col} {dfn}")
            except Exception:
                pass
        conn.commit()

init_db()

def pedido_to_dict(row):
    d = dict(row)
    d["anonimo"]    = bool(d.get("anonimo", 0))
    d["finalizado"] = bool(d.get("finalizado", 0))
    d.pop("comprovante", None)   # nunca expor base64 nas listagens públicas
    return d

def limpar_finalizados():
    """Exclui pedidos finalizados há mais de 24 horas."""
    limite = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "DELETE FROM pedidos WHERE finalizado=1 AND data_finalizado <= ?",
            (limite,)
        )
        conn.commit()

def is_admin():
    return session.get("admin") is True

def require_admin():
    if not is_admin():
        return redirect(url_for("login"))
    return None

# ── PÚBLICAS ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", pacotes=PACOTES,
                           pix_chave=PIX_CHAVE, pix_nome=PIX_NOME,
                           is_admin=is_admin())

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

@app.route("/pedidos")
def listar_pedidos():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,destinatario,pacote_nome,emoji,preco,data_pedido "
            "FROM pedidos WHERE status='pago' AND finalizado=0 ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/criar-pedido", methods=["POST"])
def criar_pedido():
    data         = request.get_json()
    remetente    = data.get("remetente","").strip()
    destinatario = data.get("destinatario","").strip()
    pacote_id    = data.get("pacote","")
    mensagem     = data.get("mensagem","").strip()
    anonimo      = bool(data.get("anonimo", False))

    if not destinatario or not pacote_id or not mensagem:
        return jsonify({"sucesso":False,"erro":"Preencha todos os campos obrigatórios."})
    if pacote_id not in PACOTES:
        return jsonify({"sucesso":False,"erro":"Pacote inválido."})
    if len(mensagem) > 500:
        return jsonify({"sucesso":False,"erro":"Mensagem muito longa (máx. 500 caracteres)."})

    pacote = PACOTES[pacote_id]
    nome_exibido = "Anônimo 💝" if anonimo else (remetente or "Anônimo 💝")
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO pedidos (remetente,destinatario,pacote_id,pacote_nome,"
            "emoji,preco,mensagem,anonimo,status,data_pedido) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (nome_exibido, destinatario, pacote_id, pacote["nome"],
             pacote["emoji"], pacote["preco"], mensagem, int(anonimo),
             "aguardando_pagamento", agora)
        )
        conn.commit()
        pedido_id = cur.lastrowid

    return jsonify({"sucesso":True,"pedido_id":pedido_id,
                    "preco":pacote["preco"],"pacote_nome":pacote["nome"],
                    "pix_chave":PIX_CHAVE,"pix_nome":PIX_NOME})

@app.route("/confirmar-pagamento", methods=["POST"])
def confirmar_pagamento():
    pedido_id   = request.form.get("pedido_id")
    comprovante = request.files.get("comprovante")
    if not pedido_id:
        return jsonify({"sucesso":False,"erro":"ID inválido."})

    comp_b64 = None
    if comprovante and comprovante.filename:
        dados = comprovante.read()
        if len(dados) > 5*1024*1024:
            return jsonify({"sucesso":False,"erro":"Comprovante muito grande (máx. 5MB)."})
        mime = comprovante.content_type or "image/jpeg"
        comp_b64 = f"data:{mime};base64," + base64.b64encode(dados).decode()

    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    with get_db() as conn:
        conn.execute(
            "UPDATE pedidos SET status='pago', comprovante=?, data_pagamento=? WHERE id=?",
            (comp_b64, agora, pedido_id)
        )
        conn.commit()
        row = conn.execute(
            "SELECT id,destinatario,pacote_nome,emoji,preco,data_pedido FROM pedidos WHERE id=?",
            (pedido_id,)
        ).fetchone()

    if not row:
        return jsonify({"sucesso":False,"erro":"Pedido não encontrado."})
    return jsonify({"sucesso":True,"pedido":dict(row)})

# ── LOGIN ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET","POST"])
def login():
    erro = None
    if request.method == "POST":
        u = request.form.get("usuario","").strip()
        p = request.form.get("senha","").strip()
        if u == ADMIN_USER and p == ADMIN_PASS:
            session["admin"] = True
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        erro = "Usuário ou senha incorretos."
    return render_template("login.html", erro=erro)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ── ADMIN (requer sessão) ──────────────────────────────────────────────────────

@app.route("/admin")
def admin_dashboard():
    redir = require_admin()
    if redir: return redir
    limpar_finalizados()
    return render_template("admin.html")

@app.route("/admin/dados")
def admin_dados():
    if not is_admin():
        return jsonify({"erro":"Não autorizado"}), 401
    limpar_finalizados()
    with get_db() as conn:
        pedidos = [dict(r) for r in
                   conn.execute("SELECT * FROM pedidos ORDER BY id DESC").fetchall()]
    for p in pedidos:
        p["anonimo"]    = bool(p.get("anonimo",0))
        p["finalizado"] = bool(p.get("finalizado",0))
        # não expõe o base64 completo na listagem, só indicador
        p["tem_comprovante"] = bool(p.get("comprovante"))
        p.pop("comprovante", None)

    total_geral = sum(p["preco"] for p in pedidos if p["status"]=="pago")
    aguardando  = sum(1 for p in pedidos if p["status"]=="aguardando_pagamento")
    finalizados = sum(1 for p in pedidos if p["finalizado"])
    por_pacote  = {}
    for p in pedidos:
        if p["status"]=="pago":
            por_pacote[p["pacote_nome"]] = por_pacote.get(p["pacote_nome"],0)+1

    return jsonify({"pedidos":pedidos,"total_geral":total_geral,
                    "total_pedidos":len([p for p in pedidos if p["status"]=="pago"]),
                    "aguardando":aguardando,"finalizados":finalizados,
                    "por_pacote":por_pacote})

@app.route("/admin/comprovante/<int:pid>")
def baixar_comprovante(pid):
    if not is_admin():
        return "Não autorizado", 401
    with get_db() as conn:
        row = conn.execute("SELECT comprovante FROM pedidos WHERE id=?", (pid,)).fetchone()
    if not row or not row["comprovante"]:
        return "Sem comprovante", 404
    # comprovante é "data:image/jpeg;base64,AAA..."
    header, b64data = row["comprovante"].split(",", 1)
    mime = header.split(":")[1].split(";")[0]
    ext  = mime.split("/")[-1].replace("jpeg","jpg")
    raw  = base64.b64decode(b64data)
    return Response(raw, mimetype=mime,
                    headers={"Content-Disposition":
                             f"attachment; filename=comprovante_{pid}.{ext}"})

@app.route("/admin/finalizar/<int:pid>", methods=["POST"])
def finalizar_pedido(pid):
    if not is_admin():
        return jsonify({"erro":"Não autorizado"}), 401
    agora_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "UPDATE pedidos SET finalizado=1, data_finalizado=? WHERE id=?",
            (agora_iso, pid)
        )
        conn.commit()
    return jsonify({"sucesso":True})

@app.route("/admin/exportar-csv")
def exportar_csv():
    if not is_admin():
        return "Não autorizado", 401
    with get_db() as conn:
        pedidos = [dict(r) for r in
                   conn.execute("SELECT * FROM pedidos ORDER BY id").fetchall()]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Status","Finalizado","Remetente","Destinatário",
                     "Pacote","Preço (R$)","Mensagem","Data Pedido","Data Pagamento","Data Finalizado"])
    for p in pedidos:
        writer.writerow([
            p["id"], p["status"], "Sim" if p.get("finalizado") else "Não",
            p["remetente"], p["destinatario"], p["pacote_nome"],
            f"{p['preco']:.2f}".replace(".",","), p["mensagem"],
            p["data_pedido"], p.get("data_pagamento",""), p.get("data_finalizado","")
        ])
    output.seek(0)
    return Response("\ufeff"+output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=pedidos_correio.csv"})

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
