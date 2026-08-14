import os
import re
import unicodedata
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

# ── Slugify (gera slug a partir de texto, sem acento) ─────────
def slugify(texto):
    texto = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore').decode('ascii')
    texto = texto.lower().strip()
    texto = re.sub(r'[^a-z0-9\s-]', '', texto)
    texto = re.sub(r'\s+', '-', texto)
    texto = re.sub(r'-+', '-', texto).strip('-')
    return texto

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


# ── Visibilidade de campos — colunas novas (idempotente) ──────
def init_visibilidade_columns():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Garante que a tabela "planos" tenha todas as colunas usadas pelo app
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS descricao TEXT DEFAULT ''")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS duracao_dias INTEGER")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS preco NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS texto_apresentacao TEXT DEFAULT ''")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS itens_inclusos TEXT DEFAULT ''")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS ordem INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS destaque BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS ativo BOOLEAN DEFAULT TRUE")
        # Padrão por PLANO: se o plano libera ou não cada campo
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS exibe_foto BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS exibe_whatsapp BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS exibe_instagram BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS exibe_site BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE planos ADD COLUMN IF NOT EXISTS exibe_regiao BOOLEAN DEFAULT TRUE")
        # Exceção por EXPOSITOR: NULL = usa o padrão do plano, TRUE = força mostrar, FALSE = força esconder
        for col in ('overr_foto', 'overr_whatsapp', 'overr_instagram', 'overr_site', 'overr_regiao'):
            cur.execute(f"ALTER TABLE expositores ADD COLUMN IF NOT EXISTS {col} BOOLEAN")
        # Aprovação de cadastros vindos do formulário público: entram como
        # ativo=TRUE mas aprovado=FALSE até alguém no admin revisar.
        cur.execute("ALTER TABLE expositores ADD COLUMN IF NOT EXISTS aprovado BOOLEAN DEFAULT FALSE")

        # ── CORREÇÃO (bug do deploy anterior) ──────────────────────
        # Quando a coluna `aprovado` foi criada, o DEFAULT FALSE marcou como
        # "não aprovado" TODOS os expositores que já existiam no banco desde
        # antes dessa feature — inclusive os que já estavam ativos e em
        # destaque. Isso fez eles sumirem do site.
        # Correção: rodamos, uma ÚNICA vez (controlada pela tabela
        # `migracoes_aplicadas`), um backfill que aprova retroativamente todo
        # expositor que já existia. Cadastros novos vindos do formulário
        # público (rota /api/expositores/cadastro) continuam entrando como
        # aprovado=FALSE normalmente, sem serem afetados por essa correção.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS migracoes_aplicadas (
                nome TEXT PRIMARY KEY,
                aplicada_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("SELECT 1 FROM migracoes_aplicadas WHERE nome = %s", ('backfill_aprovado_expositores',))
        ja_corrigido = cur.fetchone() is not None

        if not ja_corrigido:
            cur.execute("UPDATE expositores SET aprovado = TRUE WHERE aprovado = FALSE")
            cur.execute("INSERT INTO migracoes_aplicadas (nome) VALUES (%s)", ('backfill_aprovado_expositores',))

        conn.commit()
        cur.close()
    except Exception as e:
        traceback.print_exc()
    finally:
        if conn: conn.close()

init_visibilidade_columns()


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


# Palavras muito comuns em português que não ajudam na busca — ignoradas
# ao extrair termos da pergunta do usuário.
STOPWORDS_PT = {
    'a', 'o', 'as', 'os', 'de', 'da', 'do', 'das', 'dos', 'em', 'no', 'na',
    'nos', 'nas', 'um', 'uma', 'uns', 'umas', 'para', 'por', 'com', 'sem',
    'que', 'se', 'sua', 'seu', 'suas', 'seus', 'ao', 'aos', 'e', 'ou', 'mas',
    'como', 'tem', 'têm', 'ter', 'sao', 'são', 'é', 'sou', 'esta', 'está',
    'estão', 'isso', 'essa', 'esse', 'essas', 'esses', 'quero', 'queria',
    'gostaria', 'vocês', 'voce', 'você', 'pode', 'poderia', 'alguem',
    'alguém', 'outro', 'outra', 'outros', 'outras', 'nenhuma', 'nenhum',
    'pessoa', 'tudo', 'algo', 'onde', 'qual', 'quais', 'quem', 'quando',
    'porque', 'tambem', 'também', 'nao', 'não', 'sim', 'ola', 'olá', 'oi',
}


def extrair_termos_busca(texto, max_termos=6):
    """Extrai palavras-chave relevantes de uma frase do usuário para busca no banco."""
    if not texto:
        return []
    texto = texto.lower()
    for ch in '?!.,;:()[]{}"\'\n\r':
        texto = texto.replace(ch, ' ')
    palavras = [p.strip() for p in texto.split() if len(p.strip()) >= 4]
    termos = [p for p in palavras if p not in STOPWORDS_PT]
    # remove duplicados mantendo ordem
    vistos = set()
    unicos = []
    for t in termos:
        if t not in vistos:
            vistos.add(t)
            unicos.append(t)
    return unicos[:max_termos]


def get_dados_site(mensagem_usuario=None):
    """Busca feiras ativas (futuras), expositores em destaque E expositores que batem
    com o que o usuário perguntou (pesquisa em TODO o banco, não só nos destaques),
    pra dar contexto real à IA."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT nome, local, data_evento, horario, taxa, organizador
            FROM feiras
            WHERE ativo = TRUE AND data_evento >= CURRENT_DATE
            ORDER BY data_evento ASC
            LIMIT 6
        """)
        feiras = [format_db_data(dict(r)) for r in cur.fetchall()]

        cur.execute("""
            SELECT e.nome, e.descricao, e.regiao, e.cidade, c.nome as categoria_nome
            FROM expositores e
            LEFT JOIN categorias c ON e.categoria_id = c.id
            WHERE e.ativo = TRUE AND e.destaque = TRUE
            ORDER BY e.nome
            LIMIT 10
        """)
        expositores_destaque = [format_db_data(dict(r)) for r in cur.fetchall()]

        # Busca dinâmica: pega palavras-chave da última mensagem do usuário e
        # procura em TODOS os expositores ativos (nome, descrição e categoria),
        # não só nos que estão marcados como "destaque".
        expositores_busca = []
        termos = extrair_termos_busca(mensagem_usuario)
        if termos:
            condicoes = []
            params = []
            for termo in termos:
                like = f"%{termo}%"
                condicoes.append("(e.nome ILIKE %s OR e.descricao ILIKE %s OR c.nome ILIKE %s)")
                params.extend([like, like, like])
            where_termos = " OR ".join(condicoes)
            sql = f"""
                SELECT e.nome, e.descricao, e.regiao, e.cidade, c.nome as categoria_nome
                FROM expositores e
                LEFT JOIN categorias c ON e.categoria_id = c.id
                WHERE e.ativo = TRUE AND ({where_termos})
                ORDER BY e.destaque DESC, e.nome
                LIMIT 20
            """
            cur.execute(sql, params)
            expositores_busca = [format_db_data(dict(r)) for r in cur.fetchall()]

        cur.close()

        # Junta busca + destaques, sem duplicar (busca tem prioridade porque é
        # mais relevante pro que o usuário pediu).
        vistos = set()
        expositores = []
        for e in expositores_busca + expositores_destaque:
            chave = e.get('nome')
            if chave and chave not in vistos:
                vistos.add(chave)
                expositores.append(e)
        expositores = expositores[:20]

        return {
            'feiras': feiras,
            'expositores': expositores,
            'busca_encontrou_algo': bool(expositores_busca),
            'busca_foi_feita': bool(termos),
        }
    except Exception:
        traceback.print_exc()
        return {'feiras': [], 'expositores': [], 'busca_encontrou_algo': False, 'busca_foi_feita': False}
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


def montar_system_prompt(config, produtos, dados_site=None):
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

    dados_site = dados_site or {}
    feiras = dados_site.get('feiras') or []
    if feiras:
        partes.append("\nFeiras/eventos ativos no site no momento (use datas e locais reais, nunca invente):")
        for f in feiras:
            linha = f"- {f.get('nome')}"
            if f.get('data_evento'):
                linha += f" | Data: {f.get('data_evento')}"
            if f.get('horario'):
                linha += f" | Horário: {f.get('horario')}"
            if f.get('local'):
                linha += f" | Local: {f.get('local')}"
            if f.get('taxa'):
                linha += f" | Taxa: {f.get('taxa')}"
            partes.append(linha)

    expositores = dados_site.get('expositores') or []
    if expositores:
        partes.append("\nExpositores encontrados no banco de dados relevantes para a conversa (cite como exemplos reais, nunca invente nomes que não estão aqui):")
        for e in expositores:
            linha = f"- {e.get('nome')}"
            if e.get('categoria_nome'):
                linha += f" | Categoria: {e.get('categoria_nome')}"
            if e.get('regiao') or e.get('cidade'):
                linha += f" | Região: {e.get('regiao') or e.get('cidade')}"
            if e.get('descricao'):
                linha += f" | {e.get('descricao')}"
            partes.append(linha)

    busca_foi_feita = dados_site.get('busca_foi_feita')
    busca_encontrou_algo = dados_site.get('busca_encontrou_algo')

    if busca_foi_feita and not busca_encontrou_algo:
        # O usuário perguntou sobre algo específico e a busca no banco não achou
        # NENHUM expositor com esse termo — aí sim é correto dizer que não tem.
        partes.append("\nA busca no banco de dados para o que a pessoa pediu não encontrou nenhum expositor correspondente. Diga isso claramente, não invente nomes, e oriente a falar pelo WhatsApp para mais opções.")
    elif feiras or expositores:
        partes.append("\nSe a pessoa perguntar sobre feiras, eventos ou expositores que não aparecem nas listas acima, diga que não tem essa informação no momento e oriente a falar pelo WhatsApp — nunca invente datas, nomes ou informações.")

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


def aplicar_visibilidade(exp):
    """Aplica as regras de visibilidade: exceção do expositor > padrão do plano > mostra por padrão."""
    def decide(campo_override, campo_exibe_plano):
        override = exp.get(campo_override)
        if override is not None:
            return bool(override)
        val = exp.get(campo_exibe_plano)
        return True if val is None else bool(val)

    if not decide('overr_foto', 'exibe_foto'):
        exp['foto_url'] = None
    if not decide('overr_whatsapp', 'exibe_whatsapp'):
        exp['whatsapp'] = None
    if not decide('overr_instagram', 'exibe_instagram'):
        exp['instagram'] = None
    if not decide('overr_site', 'exibe_site'):
        exp['site_url'] = None
    if not decide('overr_regiao', 'exibe_regiao'):
        exp['regiao'] = None

    # Se estiver bloqueado (trial expirado), esconde os contatos independente de tudo
    if expositor_bloqueado(exp):
        exp['whatsapp']  = None
        exp['instagram'] = None
        exp['site_url']  = None
        exp['regiao']    = None

    return exp


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
                   p.nome as plano_nome, p.exibe_whatsapp, p.exibe_instagram, p.exibe_foto,
                   p.exibe_regiao, p.exibe_site
            FROM expositores e
            LEFT JOIN categorias c ON e.categoria_id = c.id
            LEFT JOIN planos p ON e.plano_id = p.id
            WHERE e.slug = %s AND e.ativo = TRUE AND e.aprovado = TRUE
        """, (slug,))
        expositor = cur.fetchone()
        cur.close()
        if not expositor:
            return "Expositor não encontrado", 404
        expositor = format_db_data(dict(expositor))
        bloqueado = expositor_bloqueado(expositor)
        expositor = aplicar_visibilidade(expositor)
        return render_template('expositor-detalhe.html', expositor=expositor, bloqueado=bloqueado)
    except Exception as e:
        traceback.print_exc()
        return "Erro ao carregar expositor", 500
    finally:
        if conn: conn.close()

@app.route('/missao')
def missao():
    return render_template('missao.html')

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

        filtros = "WHERE e.ativo = TRUE AND e.aprovado = TRUE"
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
                   p.nome as plano_nome, p.exibe_whatsapp, p.exibe_instagram, p.exibe_foto,
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
            exp = aplicar_visibilidade(exp)
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
                   p.nome as plano_nome, p.exibe_whatsapp, p.exibe_instagram, p.exibe_foto,
                   p.exibe_regiao, p.exibe_site
            FROM expositores e
            LEFT JOIN categorias c ON e.categoria_id = c.id
            LEFT JOIN planos p ON e.plano_id = p.id
            WHERE e.slug = %s AND e.ativo = TRUE AND e.aprovado = TRUE
        """, (slug,))
        exp = cur.fetchone()
        cur.close()
        if not exp:
            return jsonify({'error': 'Não encontrado'}), 404
        exp = format_db_data(dict(exp))
        bloqueado = expositor_bloqueado(exp)
        exp = aplicar_visibilidade(exp)
        exp['bloqueado'] = bloqueado
        return jsonify(exp)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar expositor'}), 500
    finally:
        if conn: conn.close()


@app.route('/api/expositores/cadastro', methods=['POST'])
def api_expositor_cadastro():
    """Cadastro público de expositor (formulário do site). Entra ativo=TRUE
    mas aprovado=FALSE — só aparece nas rotas públicas depois de revisado
    no admin."""
    conn = None
    try:
        data            = request.get_json()
        nome            = (data.get('nome') or '').strip()
        whatsapp        = (data.get('whatsapp') or '').strip()
        regiao          = (data.get('regiao') or '').strip()
        categoria_id    = data.get('categoria_id') or None
        categoria_outra = (data.get('categoria_outra') or '').strip()

        if not nome or not whatsapp or not regiao:
            return jsonify({'ok': False, 'error': 'Nome, WhatsApp e região são obrigatórios'}), 400

        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Categoria "Outra": reaproveita categoria existente com mesmo nome
        # (case-insensitive) ou cria uma nova.
        if not categoria_id and categoria_outra:
            cur.execute("SELECT id FROM categorias WHERE LOWER(nome) = LOWER(%s)", (categoria_outra,))
            existente = cur.fetchone()
            if existente:
                categoria_id = existente['id']
            else:
                cur.execute("""
                    INSERT INTO categorias (nome, slug, icone_url, ativo)
                    VALUES (%s,%s,%s,%s) RETURNING id
                """, (categoria_outra, slugify(categoria_outra), '', True))
                categoria_id = cur.fetchone()['id']

        # Slug único a partir do nome
        base_slug = slugify(nome) or 'expositor'
        slug = base_slug
        sufixo = 2
        while True:
            cur.execute("SELECT id FROM expositores WHERE slug = %s", (slug,))
            if not cur.fetchone():
                break
            slug = f"{base_slug}-{sufixo}"
            sufixo += 1

        cur.execute("""
            INSERT INTO expositores (nome, slug, categoria_id, regiao, cidade, whatsapp, ativo, aprovado, destaque)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (nome, slug, categoria_id, regiao, 'São Paulo', whatsapp, True, False, False))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id, 'slug': slug})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': 'Erro ao enviar cadastro'}), 500
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


@app.route('/api/admin/regioes')
@login_required
def api_admin_regioes():
    # Igual ao /api/regioes público, mas considera TODOS os expositores
    # (inclusive inativos), para sugerir no admin qualquer região já usada
    # alguma vez — inclusive uma recém-digitada pela própria cliente.
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT DISTINCT TRIM(regiao) AS nome
            FROM expositores
            WHERE regiao IS NOT NULL AND TRIM(regiao) <> ''
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
#  API — PLANOS (pública, para a tabela comparativa do site)
# ════════════════════════════════════════════════════════════

@app.route('/api/planos')
def api_planos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM planos WHERE ativo = TRUE ORDER BY ordem, id")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar planos'}), 500
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
#  API — BUSCA (barra de busca do site)
# ════════════════════════════════════════════════════════════

@app.route('/api/busca')
def api_busca():
    termo = (request.args.get('q') or '').strip()
    resultado = {'expositores': [], 'feiras': [], 'posts': []}

    if len(termo) < 2:
        return jsonify(resultado)

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        like = f"%{termo}%"

        cur.execute("""
            SELECT e.nome, e.slug, c.nome AS categoria_nome
            FROM expositores e
            LEFT JOIN categorias c ON e.categoria_id = c.id
            WHERE e.ativo = TRUE AND e.aprovado = TRUE
              AND (e.nome ILIKE %s OR c.nome ILIKE %s)
            ORDER BY e.destaque DESC, e.nome
            LIMIT 6
        """, (like, like))
        resultado['expositores'] = [format_db_data(dict(r)) for r in cur.fetchall()]

        cur.execute("""
            SELECT nome, local, data_evento
            FROM feiras
            WHERE ativo = TRUE AND data_evento >= CURRENT_DATE
              AND (nome ILIKE %s OR local ILIKE %s)
            ORDER BY data_evento ASC
            LIMIT 4
        """, (like, like))
        resultado['feiras'] = [format_db_data(dict(r)) for r in cur.fetchall()]

        cur.execute("""
            SELECT titulo, subtitulo, slug
            FROM posts
            WHERE ativo = TRUE AND (titulo ILIKE %s OR subtitulo ILIKE %s)
            ORDER BY criado_em DESC
            LIMIT 4
        """, (like, like))
        resultado['posts'] = [format_db_data(dict(r)) for r in cur.fetchall()]

        cur.close()
        return jsonify(resultado)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar'}), 500
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
                regiao, cidade, whatsapp, instagram, site_url, ativo, destaque, data_expiracao,
                overr_foto, overr_whatsapp, overr_instagram, overr_site, overr_regiao, aprovado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            data.get('nome',''), data.get('slug',''),
            data.get('categoria_id') or None, data.get('plano_id') or None,
            data.get('descricao',''), data.get('foto_url',''),
            data.get('regiao',''), data.get('cidade','São Paulo'),
            data.get('whatsapp',''), data.get('instagram',''),
            data.get('site_url',''),
            data.get('ativo', True), data.get('destaque', False),
            data.get('data_expiracao') or None,
            data.get('overr_foto'), data.get('overr_whatsapp'),
            data.get('overr_instagram'), data.get('overr_site'), data.get('overr_regiao'),
            # Cadastro feito pelo admin entra sempre já aprovado — só o
            # formulário público (rota /api/expositores/cadastro) usa FALSE.
            data.get('aprovado', True)
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
                site_url=%s, ativo=%s, destaque=%s, data_expiracao=%s,
                overr_foto=%s, overr_whatsapp=%s, overr_instagram=%s, overr_site=%s, overr_regiao=%s,
                aprovado=%s
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
            data.get('overr_foto'), data.get('overr_whatsapp'),
            data.get('overr_instagram'), data.get('overr_site'), data.get('overr_regiao'),
            data.get('aprovado', True),
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


@app.route('/api/admin/expositores/pendentes', methods=['GET'])
@login_required
def api_admin_expositores_pendentes():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT e.*, c.nome as categoria_nome
            FROM expositores e
            LEFT JOIN categorias c ON e.categoria_id = c.id
            WHERE e.aprovado = FALSE
            ORDER BY e.criado_em DESC
        """)
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
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

@app.route('/api/admin/planos', methods=['GET', 'POST'])
@login_required
def api_admin_planos():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM planos ORDER BY ordem, id")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO planos (
                nome, descricao, duracao_dias, preco, texto_apresentacao,
                itens_inclusos, ordem, destaque, ativo,
                exibe_foto, exibe_whatsapp, exibe_instagram, exibe_site, exibe_regiao
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('nome', ''), data.get('descricao', ''), data.get('duracao_dias') or None,
            data.get('preco', 0), data.get('texto_apresentacao', ''),
            data.get('itens_inclusos', ''), data.get('ordem', 0), data.get('destaque', False),
            data.get('ativo', True),
            data.get('exibe_foto', True), data.get('exibe_whatsapp', True),
            data.get('exibe_instagram', True), data.get('exibe_site', True),
            data.get('exibe_regiao', True)
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


@app.route('/api/admin/planos/<int:plano_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_plano(plano_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM planos WHERE id = %s", (plano_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE planos SET
                nome=%s, descricao=%s, duracao_dias=%s, preco=%s, texto_apresentacao=%s,
                itens_inclusos=%s, ordem=%s, destaque=%s, ativo=%s,
                exibe_foto=%s, exibe_whatsapp=%s, exibe_instagram=%s, exibe_site=%s, exibe_regiao=%s
            WHERE id=%s
        """, (
            data.get('nome', ''), data.get('descricao', ''), data.get('duracao_dias') or None,
            data.get('preco', 0), data.get('texto_apresentacao', ''),
            data.get('itens_inclusos', ''), data.get('ordem', 0), data.get('destaque', False),
            data.get('ativo', True),
            data.get('exibe_foto', True), data.get('exibe_whatsapp', True),
            data.get('exibe_instagram', True), data.get('exibe_site', True),
            data.get('exibe_regiao', True), plano_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/planos/<int:plano_id>/visibilidade', methods=['PUT'])
@login_required
def api_admin_plano_visibilidade(plano_id):
    conn = None
    try:
        data = request.get_json()
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE planos SET
                exibe_foto=%s, exibe_whatsapp=%s, exibe_instagram=%s,
                exibe_site=%s, exibe_regiao=%s
            WHERE id=%s
        """, (
            data.get('exibe_foto', True), data.get('exibe_whatsapp', True),
            data.get('exibe_instagram', True), data.get('exibe_site', True),
            data.get('exibe_regiao', True), plano_id
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
#  API ADMIN — SOS CORPORATIVA (outro negócio, mesmo admin)
#  Tabelas: sos_produtos, sos_posts, sos_leads (ver app_sos.py)
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/sos/produtos', methods=['GET', 'POST'])
@login_required
def api_admin_sos_produtos():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM sos_produtos ORDER BY ordem ASC, criado_em DESC")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO sos_produtos (nome, slug, descricao, descricao_completa, imagem, ativo, ordem)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('nome', ''), data.get('slug', ''),
            data.get('descricao', ''), data.get('descricao_completa', ''),
            data.get('imagem', ''), data.get('ativo', True),
            data.get('ordem', 0)
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


@app.route('/api/admin/sos/produtos/<int:produto_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_sos_produto(produto_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM sos_produtos WHERE id = %s", (produto_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE sos_produtos SET nome=%s, slug=%s, descricao=%s,
            descricao_completa=%s, imagem=%s, ativo=%s, ordem=%s WHERE id=%s
        """, (
            data.get('nome', ''), data.get('slug', ''),
            data.get('descricao', ''), data.get('descricao_completa', ''),
            data.get('imagem', ''), data.get('ativo', True),
            data.get('ordem', 0), produto_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/sos/posts', methods=['GET', 'POST'])
@login_required
def api_admin_sos_posts():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("SELECT * FROM sos_posts ORDER BY criado_em DESC")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO sos_posts (titulo, slug, resumo, conteudo, capa, categoria, data, publicado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('titulo', ''), data.get('slug', ''),
            data.get('resumo', ''), data.get('conteudo', ''),
            data.get('capa', ''), data.get('categoria', ''),
            data.get('data', ''), data.get('publicado', True)
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


@app.route('/api/admin/sos/posts/<int:post_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_sos_post(post_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        if request.method == 'DELETE':
            cur.execute("DELETE FROM sos_posts WHERE id = %s", (post_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE sos_posts SET titulo=%s, slug=%s, resumo=%s, conteudo=%s,
            capa=%s, categoria=%s, data=%s, publicado=%s WHERE id=%s
        """, (
            data.get('titulo', ''), data.get('slug', ''),
            data.get('resumo', ''), data.get('conteudo', ''),
            data.get('capa', ''), data.get('categoria', ''),
            data.get('data', ''), data.get('publicado', True), post_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/sos/leads', methods=['GET'])
@login_required
def api_admin_sos_leads():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM sos_leads ORDER BY criado_em DESC")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/sos/leads/<int:lead_id>', methods=['DELETE'])
@login_required
def api_admin_sos_lead(lead_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM sos_leads WHERE id = %s", (lead_id,))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
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

        # Pega a última mensagem do usuário pra usar como base da busca
        # dinâmica de expositores no banco (não só os marcados como destaque).
        ultima_mensagem_usuario = ''
        for m in reversed(historico):
            if m.get('role') == 'user' and m.get('content'):
                ultima_mensagem_usuario = m.get('content')
                break

        produtos = get_ia_produtos()
        dados_site = get_dados_site(ultima_mensagem_usuario)
        system_prompt = montar_system_prompt(config, produtos, dados_site)

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
        cur.execute("SELECT slug FROM expositores WHERE ativo = TRUE AND aprovado = TRUE AND slug IS NOT NULL")
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


# ════════════════════════════════════════════════════════════
#  SOS CORPORATIVA — montado no mesmo servidor, prefixo /sos
#  (outro negócio da mesma dona, painel acessado via aba dentro
#  do admin da Oficina, através do iframe apontando /sos/admin)
# ════════════════════════════════════════════════════════════
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from sos_app import app as sos_app

# 'application' é o objeto que o servidor (gunicorn) precisa apontar agora,
# em vez de 'app' sozinho — ele decide, pela URL, se manda a requisição
# pro app da Oficina ou pro app do SOS.
application = DispatcherMiddleware(app, {
    '/sos': sos_app,
})


if __name__ == '__main__':
    from werkzeug.serving import run_simple
    port = int(os.environ.get("PORT", 10000))
    # Rodando local com os dois apps juntos (Oficina na raiz, SOS em /sos)
    run_simple("0.0.0.0", port, application, use_reloader=True, use_debugger=True)
