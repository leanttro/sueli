import os
import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, send_from_directory, render_template, make_response, session, redirect, url_for
from dotenv import load_dotenv
from flask_cors import CORS
import datetime
import traceback
import decimal
import bcrypt

load_dotenv()

app = Flask(__name__, static_folder='.', static_url_path='', template_folder='templates')
app.secret_key = os.getenv('SECRET_KEY', 'eventos-secret-key-2025')
CORS(app)

# ── Conexão ──────────────────────────────────────────────────
def get_db_connection():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    return conn

# ── Formatador de dados (datas, decimais) ────────────────────
def format_db_data(data_dict):
    if not isinstance(data_dict, dict):
        return data_dict
    formatted = {}
    for key, value in data_dict.items():
        if isinstance(value, datetime.date):
            formatted[key] = value.strftime('%d/%m/%Y') if value else None
        elif isinstance(value, decimal.Decimal):
            try:
                formatted[key] = float(value)
            except (TypeError, ValueError):
                formatted[key] = None
        else:
            formatted[key] = value
    return formatted

# ── Auth helper ──────────────────────────────────────────────
def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_id'):
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated

# ── Verifica se expositor tem infos bloqueadas ───────────────
def expositor_bloqueado(expositor):
    """Retorna True se o período graça expirou e o plano bloqueia as infos."""
    if not expositor:
        return False
    data_exp = expositor.get('data_expiracao')
    plano_id = expositor.get('plano_id')
    # Se tem data de expiração e já passou, bloqueia
    if data_exp:
        if isinstance(data_exp, str):
            try:
                data_exp = datetime.datetime.strptime(data_exp, '%d/%m/%Y')
            except:
                return False
        if data_exp < datetime.datetime.now():
            return True
    return False


# ════════════════════════════════════════════════════════════
#  ROTAS DE PÁGINAS HTML
# ════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/expositores/<slug>')
def expositor_detalhe(slug):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT e.*, c.nome as categoria_nome, c.slug as categoria_slug,
                   p.nome as plano_nome, p.exibe_whatsapp, p.exibe_instagram,
                   p.exibe_regiao, p.exibe_site
            FROM expositores e
            LEFT JOIN categorias c ON e.categoria_id = c.id
            LEFT JOIN planos p ON e.plano_id = p.id
            WHERE e.slug = %s AND e.ativo = TRUE
        """, (slug,))
        expositor = cur.fetchone()
        cur.close()
        if not expositor:
            return "Expositor não encontrado", 404
        expositor = format_db_data(dict(expositor))
        bloqueado = expositor_bloqueado(expositor)
        return render_template('expositor-detalhe.html', expositor=expositor, bloqueado=bloqueado)
    except Exception as e:
        traceback.print_exc()
        return "Erro ao carregar expositor", 500
    finally:
        if conn: conn.close()

@app.route('/blog')
def blog():
    return render_template('blog.html')

@app.route('/blog/<slug>')
def blog_post(slug):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM posts WHERE slug = %s AND ativo = TRUE", (slug,))
        post = cur.fetchone()
        cur.close()
        if not post:
            return "Post não encontrado", 404
        return render_template('post-detalhe.html', post=format_db_data(dict(post)))
    except Exception as e:
        traceback.print_exc()
        return "Erro ao carregar post", 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — EXPOSITORES
# ════════════════════════════════════════════════════════════

@app.route('/api/expositores')
def api_expositores():
    conn = None
    try:
        categoria_slug = request.args.get('categoria')
        regiao         = request.args.get('regiao')
        cidade         = request.args.get('cidade')

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        filtros = "WHERE e.ativo = TRUE"
        params  = []

        if categoria_slug:
            filtros += " AND c.slug = %s"
            params.append(categoria_slug)
        if regiao:
            filtros += " AND e.regiao = %s"
            params.append(regiao)
        if cidade:
            filtros += " AND e.cidade = %s"
            params.append(cidade)

        cur.execute(f"""
            SELECT e.*, c.nome as categoria_nome, c.slug as categoria_slug,
                   p.nome as plano_nome, p.exibe_whatsapp, p.exibe_instagram,
                   p.exibe_regiao, p.exibe_site
            FROM expositores e
            LEFT JOIN categorias c ON e.categoria_id = c.id
            LEFT JOIN planos p ON e.plano_id = p.id
            {filtros}
            ORDER BY e.destaque DESC, e.nome
        """, params)

        rows = []
        for r in cur.fetchall():
            exp = format_db_data(dict(r))
            bloqueado = expositor_bloqueado(exp)
            # Se bloqueado, oculta as infos de contato
            if bloqueado:
                exp['whatsapp']  = None
                exp['instagram'] = None
                exp['site_url']  = None
                exp['regiao']    = None
            exp['bloqueado'] = bloqueado
            rows.append(exp)

        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar expositores'}), 500
    finally:
        if conn: conn.close()


@app.route('/api/expositores/<slug>')
def api_expositor(slug):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT e.*, c.nome as categoria_nome, c.slug as categoria_slug,
                   p.nome as plano_nome, p.exibe_whatsapp, p.exibe_instagram,
                   p.exibe_regiao, p.exibe_site
            FROM expositores e
            LEFT JOIN categorias c ON e.categoria_id = c.id
            LEFT JOIN planos p ON e.plano_id = p.id
            WHERE e.slug = %s AND e.ativo = TRUE
        """, (slug,))
        exp = cur.fetchone()
        cur.close()
        if not exp:
            return jsonify({'error': 'Não encontrado'}), 404
        exp = format_db_data(dict(exp))
        bloqueado = expositor_bloqueado(exp)
        if bloqueado:
            exp['whatsapp']  = None
            exp['instagram'] = None
            exp['site_url']  = None
            exp['regiao']    = None
        exp['bloqueado'] = bloqueado
        return jsonify(exp)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar expositor'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — CATEGORIAS
# ════════════════════════════════════════════════════════════

@app.route('/api/categorias')
def api_categorias():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM categorias WHERE ativo = TRUE ORDER BY nome")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar categorias'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — SERVIÇOS
# ════════════════════════════════════════════════════════════

@app.route('/api/servicos')
def api_servicos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM servicos WHERE ativo = TRUE ORDER BY ordem, titulo")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar serviços'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — DEPOIMENTOS
# ════════════════════════════════════════════════════════════

@app.route('/api/depoimentos')
def api_depoimentos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM depoimentos WHERE ativo = TRUE ORDER BY criado_em DESC")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar depoimentos'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — BLOG
# ════════════════════════════════════════════════════════════

@app.route('/api/blog')
def api_blog():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM posts WHERE ativo = TRUE ORDER BY criado_em DESC")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar posts'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — CONTATO (formulário da landing)
# ════════════════════════════════════════════════════════════

@app.route('/api/contato', methods=['POST'])
def api_contato():
    conn = None
    try:
        data     = request.get_json()
        nome     = (data.get('nome') or '').strip()
        email    = (data.get('email') or '').strip()
        telefone = (data.get('telefone') or '').strip()
        mensagem = (data.get('mensagem') or '').strip()

        if not nome or not telefone:
            return jsonify({'ok': False, 'error': 'Nome e telefone são obrigatórios'}), 400

        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO contatos (nome, email, telefone, mensagem)
            VALUES (%s, %s, %s, %s)
        """, (nome, email, telefone, mensagem))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': 'Erro ao salvar contato'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  ADMIN — LOGIN / LOGOUT
# ════════════════════════════════════════════════════════════

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        data  = request.get_json()
        email = data.get('email', '').strip()
        senha = data.get('senha', '')
        conn  = None
        try:
            conn = get_db_connection()
            cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM usuarios WHERE email = %s", (email,))
            user = cur.fetchone()
            cur.close()
            if user and bcrypt.checkpw(senha.encode('utf-8'), user['senha_hash'].encode('utf-8')):
                session['admin_id']   = user['id']
                session['admin_nome'] = user['nome']
                return jsonify({'ok': True})
            return jsonify({'ok': False, 'error': 'E-mail ou senha incorretos'}), 401
        except Exception as e:
            traceback.print_exc()
            return jsonify({'error': 'Erro interno'}), 500
        finally:
            if conn: conn.close()
    return render_template('admin/login.html')

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect('/admin/login')

@app.route('/admin')
@login_required
def admin_index():
    return render_template('admin/index.html', nome=session.get('admin_nome'))


# ════════════════════════════════════════════════════════════
#  API ADMIN — EXPOSITORES
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/expositores', methods=['GET', 'POST'])
@login_required
def api_admin_expositores():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("""
                SELECT e.*, c.nome as categoria_nome, p.nome as plano_nome
                FROM expositores e
                LEFT JOIN categorias c ON e.categoria_id = c.id
                LEFT JOIN planos p ON e.plano_id = p.id
                ORDER BY e.criado_em DESC
            """)
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO expositores (nome, slug, categoria_id, plano_id, descricao, foto_url,
                regiao, cidade, whatsapp, instagram, site_url, ativo, destaque, data_expiracao)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            data.get('nome',''), data.get('slug',''),
            data.get('categoria_id') or None, data.get('plano_id') or None,
            data.get('descricao',''), data.get('foto_url',''),
            data.get('regiao',''), data.get('cidade','São Paulo'),
            data.get('whatsapp',''), data.get('instagram',''),
            data.get('site_url',''),
            data.get('ativo', True), data.get('destaque', False),
            data.get('data_expiracao') or None
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/expositores/<int:exp_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_expositor(exp_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'DELETE':
            cur.execute("DELETE FROM expositores WHERE id = %s", (exp_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE expositores SET
                nome=%s, slug=%s, categoria_id=%s, plano_id=%s, descricao=%s,
                foto_url=%s, regiao=%s, cidade=%s, whatsapp=%s, instagram=%s,
                site_url=%s, ativo=%s, destaque=%s, data_expiracao=%s
            WHERE id=%s
        """, (
            data.get('nome',''), data.get('slug',''),
            data.get('categoria_id') or None, data.get('plano_id') or None,
            data.get('descricao',''), data.get('foto_url',''),
            data.get('regiao',''), data.get('cidade','São Paulo'),
            data.get('whatsapp',''), data.get('instagram',''),
            data.get('site_url',''),
            data.get('ativo', True), data.get('destaque', False),
            data.get('data_expiracao') or None,
            exp_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — CATEGORIAS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/categorias', methods=['GET', 'POST'])
@login_required
def api_admin_categorias():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM categorias ORDER BY nome")
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO categorias (nome, slug, icone_url, ativo)
            VALUES (%s,%s,%s,%s) RETURNING id
        """, (data['nome'], data['slug'], data.get('icone_url',''), data.get('ativo', True)))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/categorias/<int:cat_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_categoria(cat_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM categorias WHERE id = %s", (cat_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE categorias SET nome=%s, slug=%s, icone_url=%s, ativo=%s WHERE id=%s
        """, (data['nome'], data['slug'], data.get('icone_url',''), data.get('ativo', True), cat_id))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — SERVIÇOS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/servicos', methods=['GET', 'POST'])
@login_required
def api_admin_servicos():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM servicos ORDER BY ordem, titulo")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO servicos (titulo, slug, descricao, icone_url, foto_url, ordem, ativo)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('titulo',''), data.get('slug',''),
            data.get('descricao',''), data.get('icone_url',''),
            data.get('foto_url',''), data.get('ordem', 0),
            data.get('ativo', True)
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/servicos/<int:serv_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_servico(serv_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM servicos WHERE id = %s", (serv_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE servicos SET titulo=%s, slug=%s, descricao=%s, icone_url=%s,
            foto_url=%s, ordem=%s, ativo=%s WHERE id=%s
        """, (
            data.get('titulo',''), data.get('slug',''),
            data.get('descricao',''), data.get('icone_url',''),
            data.get('foto_url',''), data.get('ordem', 0),
            data.get('ativo', True), serv_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — DEPOIMENTOS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/depoimentos', methods=['GET', 'POST'])
@login_required
def api_admin_depoimentos():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM depoimentos ORDER BY criado_em DESC")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO depoimentos (nome, cargo, texto, foto_url, ativo)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('nome',''), data.get('cargo',''),
            data.get('texto',''), data.get('foto_url',''),
            data.get('ativo', True)
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/depoimentos/<int:dep_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_depoimento(dep_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM depoimentos WHERE id = %s", (dep_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE depoimentos SET nome=%s, cargo=%s, texto=%s, foto_url=%s, ativo=%s WHERE id=%s
        """, (
            data.get('nome',''), data.get('cargo',''),
            data.get('texto',''), data.get('foto_url',''),
            data.get('ativo', True), dep_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — BLOG
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/blog', methods=['GET', 'POST'])
@login_required
def api_admin_blog():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM posts ORDER BY criado_em DESC")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO posts (titulo, slug, subtitulo, autor, conteudo, imagem_url, ativo)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('titulo',''), data.get('slug',''),
            data.get('subtitulo',''), data.get('autor',''),
            data.get('conteudo',''), data.get('imagem_url',''),
            data.get('ativo', True)
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/blog/<int:post_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_post(post_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM posts WHERE id = %s", (post_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE posts SET titulo=%s, slug=%s, subtitulo=%s, autor=%s,
            conteudo=%s, imagem_url=%s, ativo=%s WHERE id=%s
        """, (
            data.get('titulo',''), data.get('slug',''),
            data.get('subtitulo',''), data.get('autor',''),
            data.get('conteudo',''), data.get('imagem_url',''),
            data.get('ativo', True), post_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — CONTATOS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/contatos', methods=['GET'])
@login_required
def api_admin_contatos():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM contatos ORDER BY criado_em DESC")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/contatos/<int:cont_id>/lido', methods=['POST'])
@login_required
def api_admin_contato_lido(cont_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("UPDATE contatos SET lido = TRUE WHERE id = %s", (cont_id,))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/contatos/<int:cont_id>', methods=['DELETE'])
@login_required
def api_admin_contato(cont_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM contatos WHERE id = %s", (cont_id,))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — PLANOS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/planos', methods=['GET'])
@login_required
def api_admin_planos():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM planos ORDER BY id")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  SITEMAP
# ════════════════════════════════════════════════════════════

@app.route('/sitemap.xml')
def sitemap():
    conn  = None
    urls  = [
        'https://www.seudominio.com.br/',
        'https://www.seudominio.com.br/blog',
    ]
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("SELECT slug FROM expositores WHERE ativo = TRUE AND slug IS NOT NULL")
        for row in cur.fetchall():
            urls.append(f'https://www.seudominio.com.br/expositores/{row[0]}')
        cur.execute("SELECT slug FROM posts WHERE ativo = TRUE AND slug IS NOT NULL")
        for row in cur.fetchall():
            urls.append(f'https://www.seudominio.com.br/blog/{row[0]}')
        cur.close()
    except Exception as e:
        print(f"AVISO: Erro ao buscar URLs para sitemap: {e}")
    finally:
        if conn: conn.close()

    xml  = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in urls:
        xml += f'  <url><loc>{url}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>\n'
    xml += '</urlset>'
    return make_response(xml, 200, {'Content-Type': 'application/xml'})


# ════════════════════════════════════════════════════════════
#  STATIC FILES
# ════════════════════════════════════════════════════════════

@app.route('/<path:path>')
def serve_static(path):
    basename = os.path.basename(path)
    if '.' not in basename:
        return "Not Found", 404
    if os.path.exists(os.path.join('.', path)):
        return send_from_directory('.', path)
    return "Not Found", 404


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
