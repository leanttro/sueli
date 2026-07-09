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
import requests

load_dotenv()

# ── Config do Chat IA (Groq) ──────────────────────────────────
GROQ_API_KEYS = [k for k in [os.getenv('GROQ_API_KEY_1'), os.getenv('GROQ_API_KEY_2')] if k]
GROQ_LIMITE_MENSAGENS = 1000

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

# ── IA / Chatbot — criação de tabelas (idempotente) ───────────
def init_ia_tables():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ia_config (
                id SERIAL PRIMARY KEY,
                persona_nome TEXT DEFAULT 'Assistente',
                modelo TEXT DEFAULT 'llama-3.1-8b-instant',
                prompt_sistema TEXT DEFAULT '',
                temperatura NUMERIC(3,2) DEFAULT 0.7,
                ativo BOOLEAN DEFAULT TRUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ia_produtos (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                preco TEXT DEFAULT '',
                descricao TEXT DEFAULT '',
                ordem INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ia_uso (
                id INTEGER PRIMARY KEY DEFAULT 1,
                total_mensagens INTEGER DEFAULT 0
            )
        """)
        cur.execute("SELECT COUNT(*) FROM ia_config")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO ia_config (persona_nome, modelo, prompt_sistema, temperatura, ativo) VALUES (%s,%s,%s,%s,%s)",
                        ('Assistente', 'llama-3.1-8b-instant', 'Você é um assistente de vendas simpático e direto.', 0.7, True))
        cur.execute("SELECT COUNT(*) FROM ia_uso")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO ia_uso (id, total_mensagens) VALUES (1, 0)")
        conn.commit()
        cur.close()
    except Exception as e:
        traceback.print_exc()
    finally:
        if conn: conn.close()

init_ia_tables()


# ── IA / Chatbot — helpers ────────────────────────────────────
def get_ia_config():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM ia_config ORDER BY id LIMIT 1")
        row = cur.fetchone()
        cur.close()
        return format_db_data(dict(row)) if row else None
    finally:
        if conn: conn.close()


def get_ia_produtos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM ia_produtos ORDER BY ordem, id")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return rows
    finally:
        if conn: conn.close()


def get_contador():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT total_mensagens FROM ia_uso WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        return row['total_mensagens'] if row else 0
    finally:
        if conn: conn.close()


def incrementar_contador():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE ia_uso SET total_mensagens = total_mensagens + 1 WHERE id = 1")
        conn.commit()
        cur.close()
    finally:
        if conn: conn.close()


def montar_system_prompt(config, produtos):
    partes = []
    persona = config.get('persona_nome') or 'Assistente'
    partes.append(f"Você é {persona}, um assistente de vendas virtual.")
    prompt_base = config.get('prompt_sistema') or ''
    if prompt_base:
        partes.append(prompt_base)
    if produtos:
        partes.append("\nProdutos/serviços disponíveis para oferecer durante a conversa:")
        for p in produtos:
            linha = f"- {p.get('nome')}"
            if p.get('preco'):
                linha += f" | Preço: {p.get('preco')}"
            if p.get('descricao'):
                linha += f" | {p.get('descricao')}"
            partes.append(linha)
        partes.append("\nUse essas informações para conduzir a conversa de forma natural e induzir a pessoa a comprar, sem ser insistente ou repetitivo.")
    return "\n".join(partes)


def chamar_groq(mensagens, config):
    """Tenta a 1ª chave Groq; se falhar, tenta a 2ª. Levanta exceção se ambas falharem."""
    if not GROQ_API_KEYS:
        raise RuntimeError('Nenhuma chave GROQ configurada no ambiente')

    modelo = config.get('modelo') or 'llama-3.1-8b-instant'
    try:
        temperatura = float(config.get('temperatura') or 0.7)
    except (TypeError, ValueError):
        temperatura = 0.7

    ultimo_erro = None
    for chave in GROQ_API_KEYS:
        try:
            resp = requests.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {chave}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': modelo,
                    'messages': mensagens,
                    'temperature': temperatura,
                    'max_tokens': 1024
                },
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                return data['choices'][0]['message']['content']
            else:
                ultimo_erro = f"Groq respondeu {resp.status_code}: {resp.text[:200]}"
                continue
        except Exception as e:
            ultimo_erro = str(e)
            continue

    raise RuntimeError(ultimo_erro or 'Falha ao chamar Groq')


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
            filtros += " AND TRIM(LOWER(e.regiao)) = TRIM(LOWER(%s))"
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


@app.route('/api/regioes')
def api_regioes():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Busca as regiões diretamente das que já estão em uso pelos
        # expositores ativos, evitando depender de uma tabela separada
        # que precisaria ser mantida manualmente e ficava dessincronizada
        # com o que é cadastrado no admin.
        cur.execute("""
            SELECT DISTINCT TRIM(regiao) AS nome
            FROM expositores
            WHERE ativo = TRUE AND regiao IS NOT NULL AND TRIM(regiao) <> ''
            ORDER BY nome
        """)
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar regiões'}), 500
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
#  API PÚBLICA — FEIRAS
# ════════════════════════════════════════════════════════════

@app.route('/api/feiras')
def api_feiras():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM feiras
            WHERE ativo = TRUE AND data_evento >= CURRENT_DATE
            ORDER BY data_evento ASC
        """)
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar feiras'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — FEIRAS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/feiras', methods=['GET', 'POST'])
@login_required
def api_admin_feiras():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM feiras ORDER BY data_evento DESC")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO feiras (nome, local, data_evento, horario, vagas_para,
                infraestrutura, informacoes, observacoes, taxa, organizador,
                whatsapp, ativo)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('nome',''), data.get('local',''),
            data.get('data_evento') or None, data.get('horario',''),
            data.get('vagas_para',''), data.get('infraestrutura',''),
            data.get('informacoes',''), data.get('observacoes',''),
            data.get('taxa',''), data.get('organizador',''),
            data.get('whatsapp',''), data.get('ativo', True)
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


@app.route('/api/admin/feiras/<int:feira_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_feira(feira_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM feiras WHERE id = %s", (feira_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE feiras SET nome=%s, local=%s, data_evento=%s, horario=%s,
                vagas_para=%s, infraestrutura=%s, informacoes=%s, observacoes=%s,
                taxa=%s, organizador=%s, whatsapp=%s, ativo=%s
            WHERE id=%s
        """, (
            data.get('nome',''), data.get('local',''),
            data.get('data_evento') or None, data.get('horario',''),
            data.get('vagas_para',''), data.get('infraestrutura',''),
            data.get('informacoes',''), data.get('observacoes',''),
            data.get('taxa',''), data.get('organizador',''),
            data.get('whatsapp',''), data.get('ativo', True),
            feira_id
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
#  API ADMIN — CONFIGURAR IA (persona, modelo, prompt, temperatura)
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/ia/config', methods=['GET', 'POST'])
@login_required
def api_admin_ia_config():
    conn = None
    try:
        if request.method == 'GET':
            return jsonify(get_ia_config() or {})

        data = request.get_json()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE ia_config SET persona_nome=%s, modelo=%s, prompt_sistema=%s,
                temperatura=%s, ativo=%s
            WHERE id = (SELECT id FROM ia_config ORDER BY id LIMIT 1)
        """, (
            data.get('persona_nome', 'Assistente'),
            data.get('modelo', 'llama-3.1-8b-instant'),
            data.get('prompt_sistema', ''),
            data.get('temperatura', 0.7),
            data.get('ativo', True)
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
#  API ADMIN — PRODUTOS DA IA
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/ia/produtos', methods=['GET', 'POST'])
@login_required
def api_admin_ia_produtos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM ia_produtos ORDER BY ordem, id")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO ia_produtos (nome, preco, descricao, ordem)
            VALUES (%s,%s,%s,%s) RETURNING id
        """, (
            data.get('nome', ''), data.get('preco', ''),
            data.get('descricao', ''), data.get('ordem', 0)
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


@app.route('/api/admin/ia/produtos/<int:prod_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_ia_produto(prod_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM ia_produtos WHERE id = %s", (prod_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE ia_produtos SET nome=%s, preco=%s, descricao=%s, ordem=%s
            WHERE id=%s
        """, (
            data.get('nome', ''), data.get('preco', ''),
            data.get('descricao', ''), data.get('ordem', 0), prod_id
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
#  API ADMIN — CONTADOR DE MENSAGENS DA IA
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/ia/contador', methods=['GET'])
@login_required
def api_admin_ia_contador():
    usado = get_contador()
    return jsonify({
        'usado': usado,
        'limite': GROQ_LIMITE_MENSAGENS,
        'restante': max(0, GROQ_LIMITE_MENSAGENS - usado),
        'chaves_configuradas': len(GROQ_API_KEYS)
    })


@app.route('/api/admin/ia/contador/resetar', methods=['POST'])
@login_required
def api_admin_ia_contador_resetar():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE ia_uso SET total_mensagens = 0 WHERE id = 1")
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API PÚBLICA — CHAT (widget do site)
# ════════════════════════════════════════════════════════════

@app.route('/api/chat', methods=['POST'])
def api_chat():
    try:
        data = request.get_json() or {}
        historico = data.get('mensagens', [])
        if not isinstance(historico, list) or not historico:
            return jsonify({'error': 'Mensagem vazia'}), 400

        # Limita histórico enviado (evita prompt gigante)
        historico = historico[-20:]

        usado = get_contador()
        if usado >= GROQ_LIMITE_MENSAGENS:
            return jsonify({
                'error': 'indisponivel',
                'reply': 'No momento o assistente está indisponível. Fale com a gente pelo WhatsApp!'
            }), 200

        config = get_ia_config()
        if not config or not config.get('ativo', True):
            return jsonify({
                'error': 'indisponivel',
                'reply': 'No momento o assistente está indisponível. Fale com a gente pelo WhatsApp!'
            }), 200

        produtos = get_ia_produtos()
        system_prompt = montar_system_prompt(config, produtos)

        mensagens_groq = [{'role': 'system', 'content': system_prompt}]
        for m in historico:
            papel = m.get('role')
            conteudo = m.get('content', '')
            if papel in ('user', 'assistant') and conteudo:
                mensagens_groq.append({'role': papel, 'content': conteudo})

        try:
            resposta = chamar_groq(mensagens_groq, config)
        except Exception as e:
            traceback.print_exc()
            return jsonify({
                'error': 'indisponivel',
                'reply': 'No momento o assistente está indisponível. Fale com a gente pelo WhatsApp!'
            }), 200

        incrementar_contador()
        return jsonify({'reply': resposta})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro interno'}), 500


# ════════════════════════════════════════════════════════════
#  SITEMAP
# ════════════════════════════════════════════════════════════

@app.route('/sitemap.xml')
def sitemap():
    conn  = None
    BASE_URL = 'https://www.oficinaempreendersp.com.br'
    urls  = [
        f'{BASE_URL}/',
        f'{BASE_URL}/blog',
    ]
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("SELECT slug FROM expositores WHERE ativo = TRUE AND slug IS NOT NULL")
        for row in cur.fetchall():
            urls.append(f'{BASE_URL}/expositores/{row[0]}')
        cur.execute("SELECT slug FROM posts WHERE ativo = TRUE AND slug IS NOT NULL")
        for row in cur.fetchall():
            urls.append(f'{BASE_URL}/blog/{row[0]}')
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
