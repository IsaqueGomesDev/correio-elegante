from flask import (Flask, render_template, request, jsonify,
                   Response, redirect, url_for, session, send_from_directory)
from datetime import datetime
import os, csv, io, base64, secrets

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

ADMIN_USER = os.environ.get("ADMIN_USER", "manoelder")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "reidosratos")
PIX_CHAVE  = os.environ.get("PIX_CHAVE",  "11999999999")
PIX_NOME   = os.environ.get("PIX_NOME",   "Terceirão 2026")
DATABASE_URL = os.environ.get("DATABASE_URL")  # PostgreSQL no Render
DB_FILE      = os.environ.get("DB_PATH", "correio.db")  # SQLite local
USE_PG = bool(DATABASE_URL)
if USE_PG:
    app.logger.warning("CONECTANDO NO SUPABASE...")
else:
    app.logger.warning("USANDO SQLITE LOCAL")

PACOTES = {
    "carta":             {"nome": "Carta Normal",          "preco": 2.00,  "emoji": "💌"},
    "carta_pirulito":    {"nome": "Carta + Pirulito",      "preco": 3.00,  "emoji": "🍭"},
    "carta_bombom":      {"nome": "Carta + Bombom",        "preco": 4.00,  "emoji": "🍫"},
    "carta_flor":        {"nome": "Carta + Flor",          "preco": 8.00,  "emoji": "🌹"},
    "carta_bombom_flor": {"nome": "Carta + Bombom e Flor", "preco": 10.00, "emoji": "🌹🍫"},
}

# ── DB (PostgreSQL ou SQLite) ───────────────────────────────────────────────

USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    def get_db():
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    PH = "%s"   # placeholder PostgreSQL
else:
    import sqlite3
    def get_db():
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn
    PH = "?"    # placeholder SQLite

CREATE_TABLE_PG = """
    CREATE TABLE IF NOT EXISTS pedidos (
        id                  SERIAL PRIMARY KEY,
        remetente           TEXT NOT NULL,
        destinatario        TEXT NOT NULL,
        turma               TEXT NOT NULL DEFAULT '',
        turno               TEXT NOT NULL DEFAULT '',
        turma_dest          TEXT NOT NULL DEFAULT '',
        turno_dest          TEXT NOT NULL DEFAULT '',
        dia_entrega         TEXT NOT NULL DEFAULT '',
        pacote_id           TEXT NOT NULL,
        pacote_nome         TEXT NOT NULL,
        emoji               TEXT NOT NULL,
        preco               REAL NOT NULL,
        adicional_serenata  INTEGER NOT NULL DEFAULT 0,
        adicional_caixa     INTEGER NOT NULL DEFAULT 0,
        musica_serenata     TEXT NOT NULL DEFAULT '',
        musica_caixa        TEXT NOT NULL DEFAULT '',
        total               REAL NOT NULL DEFAULT 0,
        mensagem            TEXT NOT NULL,
        anonimo             INTEGER NOT NULL DEFAULT 0,
        status              TEXT NOT NULL DEFAULT 'aguardando_pagamento',
        comprovante         TEXT,
        data_pedido         TEXT NOT NULL,
        data_pagamento      TEXT,
        finalizado          INTEGER NOT NULL DEFAULT 0,
        data_finalizado     TEXT
    )
"""

CREATE_TABLE_SQLITE = CREATE_TABLE_PG\
    .replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")\
    .replace("REAL", "REAL")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    sql = CREATE_TABLE_PG if USE_PG else CREATE_TABLE_SQLITE
    cur.execute(sql)
    if not USE_PG:
        # adiciona colunas novas em bancos SQLite antigos
        for col, dfn in [
            ("turma","TEXT NOT NULL DEFAULT ''"),
            ("turno","TEXT NOT NULL DEFAULT ''"),
            ("turma_dest","TEXT NOT NULL DEFAULT ''"),
            ("turno_dest","TEXT NOT NULL DEFAULT ''"),
            ("dia_entrega","TEXT NOT NULL DEFAULT ''"),
            ("adicional_serenata","INTEGER NOT NULL DEFAULT 0"),
            ("adicional_caixa","INTEGER NOT NULL DEFAULT 0"),
            ("musica_serenata","TEXT NOT NULL DEFAULT ''"),
            ("musica_caixa","TEXT NOT NULL DEFAULT ''"),
            ("total","REAL NOT NULL DEFAULT 0"),
            ("finalizado","INTEGER NOT NULL DEFAULT 0"),
            ("data_finalizado","TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE pedidos ADD COLUMN {col} {dfn}")
            except Exception:
                pass
    conn.commit()
    conn.close()

init_db()

def row_to_dict(row, cursor=None):
    if USE_PG:
        cols = [d[0] for d in cursor.description]
        d = dict(zip(cols, row))
    else:
        d = dict(row)
    d["anonimo"]    = bool(d.get("anonimo", 0))
    d["finalizado"] = bool(d.get("finalizado", 0))
    d["adicional_serenata"] = bool(d.get("adicional_serenata", 0))
    d["adicional_caixa"]    = bool(d.get("adicional_caixa", 0))
    return d

def rows_to_dicts(rows, cursor):
    return [row_to_dict(r, cursor) for r in rows]

# ── AUTH ───────────────────────────────────────────────────────────────────────

def is_admin():
    return session.get("admin") is True

def require_admin():
    if not is_admin():
        return redirect(url_for("login"))
    return None

# ── ROTAS PÚBLICAS ─────────────────────────────────────────────────────────────

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
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "SELECT id,destinatario,pacote_nome,emoji,preco,data_pedido "
        "FROM pedidos WHERE status='pago' AND finalizado=0 ORDER BY id DESC LIMIT 50"
    )
    rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/criar-pedido", methods=["POST"])
def criar_pedido():
    data         = request.get_json()
    remetente    = data.get("remetente","").strip()
    destinatario = data.get("destinatario","").strip()
    turma        = data.get("turma","").strip()
    turno        = data.get("turno","").strip()
    turma_dest   = data.get("turma_dest","").strip()
    turno_dest   = data.get("turno_dest","").strip()
    dia_entrega  = data.get("dia_entrega","").strip()
    pacote_id    = data.get("pacote","")
    mensagem     = data.get("mensagem","").strip()
    anonimo      = bool(data.get("anonimo", False))
    ad_serenata  = bool(data.get("adicional_serenata", False))
    ad_caixa     = bool(data.get("adicional_caixa", False))
    musica_ser   = data.get("musica_serenata","").strip()
    musica_cai   = data.get("musica_caixa","").strip()

    if not destinatario or not pacote_id or not mensagem:
        return jsonify({"sucesso":False,"erro":"Preencha todos os campos obrigatórios."})
    if not turma or not turno:
        return jsonify({"sucesso":False,"erro":"Informe sua turma e turno."})
    if not turma_dest or not turno_dest:
        return jsonify({"sucesso":False,"erro":"Informe a turma e turno do destinatário."})
    if not dia_entrega:
        return jsonify({"sucesso":False,"erro":"Escolha o dia de entrega."})
    if pacote_id not in PACOTES:
        return jsonify({"sucesso":False,"erro":"Pacote inválido."})
    if len(mensagem) > 500:
        return jsonify({"sucesso":False,"erro":"Mensagem muito longa (máx. 500 caracteres)."})

    pacote = PACOTES[pacote_id]
    total  = pacote["preco"] + (4.0 if ad_serenata else 0) + (3.0 if ad_caixa else 0)
    nome_exibido = "Anônimo 💝" if anonimo else (remetente or "Anônimo 💝")
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")

    conn = get_db(); cur = conn.cursor()
    cur.execute(
        f"INSERT INTO pedidos (remetente,destinatario,turma,turno,turma_dest,turno_dest,"
        f"dia_entrega,pacote_id,pacote_nome,emoji,preco,adicional_serenata,adicional_caixa,"
        f"musica_serenata,musica_caixa,total,mensagem,anonimo,status,data_pedido) "
        f"VALUES ({','.join([PH]*20)})",
        (nome_exibido, destinatario, turma, turno, turma_dest, turno_dest,
         dia_entrega, pacote_id, pacote["nome"], pacote["emoji"], pacote["preco"],
         int(ad_serenata), int(ad_caixa), musica_ser, musica_cai, total,
         mensagem, int(anonimo), "aguardando_pagamento", agora)
    )
    conn.commit()
    if USE_PG:
        cur.execute("SELECT lastval()")
        pedido_id = cur.fetchone()[0]
    else:
        pedido_id = cur.lastrowid
    conn.close()

    return jsonify({"sucesso":True,"pedido_id":pedido_id,
                    "preco":total,"pacote_nome":pacote["nome"],
                    "pix_chave":PIX_CHAVE,"pix_nome":PIX_NOME})

@app.route("/confirmar-pagamento", methods=["POST"])
def confirmar_pagamento():
    pedido_id   = request.form.get("pedido_id")
    comprovante = request.files.get("comprovante")

    if not pedido_id:
        return jsonify({"sucesso":False,"erro":"ID inválido."})
    if not comprovante or not comprovante.filename:
        return jsonify({"sucesso":False,"erro":"Comprovante obrigatório. Envie a foto do pagamento."})

    dados = comprovante.read()
    if len(dados) > 5*1024*1024:
        return jsonify({"sucesso":False,"erro":"Comprovante muito grande (máx. 5MB)."})
    mime = comprovante.content_type or "image/jpeg"
    comp_b64 = f"data:{mime};base64," + base64.b64encode(dados).decode()

    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        f"UPDATE pedidos SET status='pago', comprovante={PH}, data_pagamento={PH} WHERE id={PH}",
        (comp_b64, agora, pedido_id)
    )
    conn.commit()
    cur.execute(
        f"SELECT id,destinatario,pacote_nome,emoji,preco,data_pedido FROM pedidos WHERE id={PH}",
        (pedido_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"sucesso":False,"erro":"Pedido não encontrado."})
    cols = ["id","destinatario","pacote_nome","emoji","preco","data_pedido"]
    return jsonify({"sucesso":True,"pedido":dict(zip(cols,row))})

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

# ── ADMIN ──────────────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_dashboard():
    redir = require_admin()
    if redir: return redir
    return render_template("admin.html")

@app.route("/admin/dados")
def admin_dados():
    if not is_admin():
        return jsonify({"erro":"Não autorizado"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM pedidos ORDER BY id DESC")
    pedidos = rows_to_dicts(cur.fetchall(), cur)
    conn.close()
    for p in pedidos:
        p["tem_comprovante"] = bool(p.get("comprovante"))
        p.pop("comprovante", None)

    total_geral = sum(p["total"] or p["preco"] for p in pedidos if p["status"]=="pago")
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
    conn = get_db(); cur = conn.cursor()
    cur.execute(f"SELECT comprovante FROM pedidos WHERE id={PH}", (pid,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return "Sem comprovante", 404
    header, b64data = row[0].split(",", 1)
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
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        f"UPDATE pedidos SET finalizado=1, data_finalizado={PH} WHERE id={PH}",
        (agora_iso, pid)
    )
    conn.commit(); conn.close()
    return jsonify({"sucesso":True})

@app.route("/admin/excluir/<int:pid>", methods=["POST"])
def excluir_pedido(pid):
    if not is_admin():
        return jsonify({"erro":"Não autorizado"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute(f"DELETE FROM pedidos WHERE id={PH}", (pid,))
    conn.commit(); conn.close()
    return jsonify({"sucesso":True})

@app.route("/admin/exportar-csv")
def exportar_csv():
    if not is_admin():
        return "Não autorizado", 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM pedidos ORDER BY id")
    pedidos = rows_to_dicts(cur.fetchall(), cur)
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Status","Finalizado","Remetente","Turma","Turno",
                     "Destinatário","Turma Dest","Turno Dest","Dia Entrega",
                     "Pacote","Serenata","Música Serenata","Caixa de Som","Música Caixa",
                     "Preço Pacote","Total","Mensagem","Data Pedido","Data Pagamento"])
    for p in pedidos:
        writer.writerow([
            p["id"], p["status"], "Sim" if p.get("finalizado") else "Não",
            p["remetente"], p.get("turma",""), p.get("turno",""),
            p["destinatario"], p.get("turma_dest",""), p.get("turno_dest",""),
            p.get("dia_entrega",""), p["pacote_nome"],
            "Sim" if p.get("adicional_serenata") else "Não", p.get("musica_serenata",""),
            "Sim" if p.get("adicional_caixa") else "Não", p.get("musica_caixa",""),
            f"{p['preco']:.2f}".replace(".",","),
            f"{p.get('total',p['preco']):.2f}".replace(".",","),
            p["mensagem"], p["data_pedido"], p.get("data_pagamento","")
        ])
    output.seek(0)
    return Response("\ufeff"+output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=pedidos_correio.csv"})

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT",5000)))