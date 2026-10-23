CREATE TABLE IF NOT EXISTS produtos (
  id_produto TEXT PRIMARY KEY,
  marketplace TEXT NOT NULL DEFAULT 'Mercado Livre',
  nome_produto TEXT NOT NULL,
  categoria TEXT,
  sub_categoria TEXT,
  imagem_url TEXT,
  url_original TEXT NOT NULL,
  url_afiliado TEXT,
  valor_original REAL,
  valor_atual REAL,
  desconto_pct REAL,
  nota REAL,
  vendas INTEGER,
  termo_origem TEXT,
  tema_origem TEXT,
  score REAL,
  afinidade REAL,
  suspeito INTEGER DEFAULT 0,
  motivo_ia TEXT,
  status TEXT NOT NULL DEFAULT 'novo'
    CHECK (status IN ('novo','curado','reserva','link_pronto','publicado','expirado','descartado','pausado')),
  prioridade TEXT DEFAULT 'Media',
  data_captura TEXT,
  data_atualizacao TEXT,
  observacao TEXT
);

CREATE TABLE IF NOT EXISTS historico_preco (
  id_produto TEXT,
  data TEXT,
  valor_atual REAL,
  valor_original REAL,
  PRIMARY KEY (id_produto, data)
);

CREATE TABLE IF NOT EXISTS temas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  data TEXT,
  tema TEXT,
  fonte TEXT,
  nota_tendencia REAL,
  justificativa TEXT,
  usado INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS buscas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  termo TEXT UNIQUE,
  tema_id INTEGER,
  nicho TEXT,
  prioridade INTEGER DEFAULT 5,
  filtros_json TEXT,
  ativo INTEGER DEFAULT 1,
  inativo_ate TEXT,
  execucoes INTEGER DEFAULT 0,
  execucoes_sem_aprovado INTEGER DEFAULT 0,
  ultima_execucao TEXT,
  resultados_total INTEGER DEFAULT 0,
  aprovados_total INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
  post_id INTEGER PRIMARY KEY AUTOINCREMENT,
  id_produto TEXT NOT NULL REFERENCES produtos(id_produto),
  nome_produto TEXT NOT NULL,
  url_afiliado TEXT NOT NULL,
  slug TEXT UNIQUE NOT NULL,
  canal TEXT DEFAULT 'telegram',
  message_id INTEGER,
  data_hora_postagem TEXT NOT NULL,
  preco_postado REAL,
  score REAL,
  termo_origem TEXT,
  copy_texto TEXT,
  modo TEXT DEFAULT 'sombra' CHECK (modo IN ('sombra','real')),
  quantidade_visualizacoes INTEGER DEFAULT 0,
  quantidade_cliques INTEGER DEFAULT 0,
  quantidade_vendido INTEGER DEFAULT 0,
  atualizacao TEXT
);

CREATE TABLE IF NOT EXISTS metricas_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER REFERENCES posts(post_id),
  tipo TEXT CHECK (tipo IN ('views','cliques','vendas','reacoes')),
  janela TEXT,
  valor INTEGER,
  coletado_em TEXT
);

CREATE TABLE IF NOT EXISTS execucoes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  modulo TEXT,
  inicio TEXT,
  fim TEXT,
  status TEXT,
  itens INTEGER,
  erro TEXT
);

CREATE TABLE IF NOT EXISTS gasto_ia (
  data TEXT,
  modelo TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  custo_estimado_brl REAL
);

CREATE INDEX IF NOT EXISTS ix_prod_status ON produtos(status, score DESC);
CREATE INDEX IF NOT EXISTS ix_posts_data ON posts(data_hora_postagem);
