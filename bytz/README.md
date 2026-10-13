# BYTZ

Projeto de curadoria de ofertas de tecnologia, setup, smart home, acessórios e fitness. O sistema acha produtos em alta, filtra os melhores, escreve a legenda e publica no Telegram. Os links são de afiliado, então o projeto ganha comissão quando alguém compra.

> **Status:** em desenvolvimento (Sprint 0, fundação).

## O que ele faz

1. **Descobre** o que vale pesquisar no dia (tendências, datas sazonais e histórico).
2. **Capta** produtos em páginas públicas e aplica filtros de qualidade (nota, vendas, desconto real).
3. **Pontua** cada produto e escolhe os melhores.
4. **Gera** o link de afiliado e a legenda, com tom leve e bem-humorado.
5. **Publica** no Telegram em intervalos de cerca de 15 minutos.
6. **Mede** visualizações, cliques e vendas para melhorar as escolhas seguintes.

## Pastas

```
bytz/
├─ back/    scripts em Python, regras de negócio, banco de dados e integrações
└─ front/   página de redirecionamento (HTML, CSS e JavaScript)
```

## Stack

Python 3.11+, SQLite, API do Gemini (IA), Telegram Bot API, HTML/CSS/JS.

## Como rodar (em construção)

1. Instalar Python 3.11 ou mais novo.
2. Criar um ambiente virtual e instalar `back/requirements.txt`.
3. Copiar `back/.env.example` para `back/.env` e preencher as variáveis.
4. Rodar `python back/scripts/doctor.py` para checar a configuração.

## Segurança

Nenhum segredo vai para o repositório. Chaves, cookies e credenciais ficam em variáveis de ambiente e em pastas fora do Git.

## Aviso

Os links de ofertas são de afiliado. O preço para quem compra é o mesmo.
