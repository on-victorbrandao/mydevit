# BYTZ

Projeto de curadoria de ofertas de tecnologia, setup, smart home, acessórios e fitness. O sistema acha produtos em alta, filtra os melhores, escreve a legenda e publica no Telegram. Os links são de afiliado, então o projeto ganha comissão quando alguém compra.

> **Status:** em desenvolvimento (Sprint 0: fundação local, banco SQLite, logs sanitizados, diagnóstico e testes).

## Arquitetura do pipeline

Por enquanto, só a fundação (banco, logs, alertas, diagnóstico e testes) está implementada; as sete etapas abaixo são o desenho do pipeline que vamos construir nas próximas sprints:

1. **Scout:** identifica temas e categorias em alta no dia (sazonalidade e histórico).
2. **Estrategista:** transforma os temas em termos de busca objetivos.
3. **Captação:** coleta dados de produtos em páginas públicas e normaliza preços, notas e volume de vendas.
4. **Curador:** aplica filtros de qualidade, acompanha histórico de preço e calcula a pontuação de cada item.
5. **Ativação:** gera o link de afiliado e monta a legenda com apoio de IA (os valores numéricos e links vêm sempre do código).
6. **Publicador:** envia as ofertas aprovadas para o canal no Telegram ao longo do dia (com suporte a modo sombra para validação prévia).
7. **Analista:** registra visualizações, cliques na página intermediária e conversões para retroalimentar as escolhas seguintes.

## Estrutura de pastas

```text
bytz/
├─ back/
│  ├─ config/          regras gerais de negócio, categorias, sazonalidade e prompts
│  ├─ core/            banco SQLite (schema.sql, db.py), logs sanitizados (logger.py) e alertas (alerts.py)
│  ├─ scripts/         utilitários operacionais e diagnóstico do ambiente (doctor.py)
│  ├─ tests/           suíte de testes automatizados e isolados (pytest)
│  ├─ config.yaml      parâmetros operacionais do pipeline
│  ├─ pyproject.toml   configuração de testes e linters (pytest, ruff, black)
│  └─ requirements.txt dependências Python
└─ front/              página intermediária de redirecionamento (HTML, CSS e JavaScript)
```

## Stack

Python 3.11+, SQLite, Google Sheets (espelho e fila operacional), API do Gemini (IA), Telegram Bot API, HTML/CSS/JS, Pytest, Ruff e Black.

## Como rodar (ambiente local)

1. Instalar Python 3.11 ou mais novo.
2. Criar o ambiente virtual em `back/.venv` e instalar `back/requirements.txt`.
3. Copiar `back/.env.example` para `back/.env` e preencher as variáveis necessárias.
4. Inicializar o banco local SQLite:
   ```powershell
   back\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'back'); from core.db import init_db; init_db()"
   ```
5. Rodar o diagnóstico do ambiente:
   ```powershell
   back\.venv\Scripts\python.exe back/scripts/doctor.py
   ```
6. Executar a suíte de testes:
   ```powershell
   back\.venv\Scripts\python.exe -m pytest back/tests -v
   ```

## Segurança

Nenhum segredo vai para o repositório. Chaves de API, tokens, cookies, banco de dados local e logs ficam fora do Git, protegidos pelo `.gitignore`, e os logs passam por sanitização automática antes de serem gravados ou exibidos.

## Aviso

Os links de ofertas são de afiliado. O preço para quem compra é o mesmo.
