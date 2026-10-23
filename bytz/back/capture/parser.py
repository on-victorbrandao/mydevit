# funcoes puras de parsing e normalizacao dos cards de busca do mercado livre
import re
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from capture import selectors

RE_ID_CATALOGO = re.compile(r"/p/(MLB-?\d+)", re.IGNORECASE)
RE_ID_WID = re.compile(r"[?&]wid=(MLB-?\d+)", re.IGNORECASE)
RE_ID_FALLBACK = re.compile(r"(MLB(?!U)-?\d+)", re.IGNORECASE)
RE_ID_MLBU = re.compile(r"/up/(MLBU-?\d+)", re.IGNORECASE)


def parse_preco(fracao: str | None, centavos: str | None = None) -> float | None:
    # monta float do preco combinando fracao sem pontos de milhar e centavos quando existirem
    if not fracao:
        return None
    limpo = re.sub(r"[^\d]", "", str(fracao))
    if not limpo:
        return None
    valor = float(int(limpo))
    if centavos:
        cents_limpo = re.sub(r"[^\d]", "", str(centavos))
        if cents_limpo:
            valor += int(cents_limpo[:2].ljust(2, "0")) / 100.0
    return round(valor, 2)


def parse_nota(texto: str | None) -> float | None:
    # converte nota com virgula ou ponto para float entre 0 e 5
    if not texto:
        return None
    normalizado = str(texto).strip().replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", normalizado)
    if not match:
        return None
    valor = float(match.group(0))
    return valor if 0.0 <= valor <= 5.0 else None


def parse_vendas(texto: str | None) -> int | None:
    # converte volume de vendas como '+5 mil vendidos' ou '+500 vendidos' para inteiro ou None se ausente
    if not texto:
        return None
    limpo = str(texto).lower().strip()
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(mil)?", limpo)
    if not match:
        return None
    numero_str = match.group(1).replace(",", ".")
    tem_mil = match.group(2) is not None
    if tem_mil:
        return int(float(numero_str) * 1000)
    return int(float(numero_str.replace(".", "")))


def extrair_id_produto(url_original: str | None) -> str | None:
    # extrai o id antes de limpar parametros na ordem /p/MLB, wid=MLB e fallback MLB ou MLBU
    if not url_original:
        return None
    texto = str(url_original)
    for padrao in (RE_ID_CATALOGO, RE_ID_WID, RE_ID_FALLBACK, RE_ID_MLBU):
        match = padrao.search(texto)
        if match:
            return match.group(1).upper().replace("-", "")
    return None


def limpar_url(url_original: str | None) -> str:
    # remove fragmentos #polycard e parametros de rastreio da url original
    if not url_original:
        return ""
    partes = urlsplit(str(url_original).strip())
    return urlunsplit((partes.scheme, partes.netloc, partes.path, "", ""))


def calcular_desconto_pct(valor_original: float | None, valor_atual: float | None) -> float:
    # calcula percentual de desconto real com base no valor original riscado e no valor atual
    if not valor_original or not valor_atual or valor_original <= 0 or valor_atual >= valor_original:
        return 0.0
    return round((valor_original - valor_atual) / valor_original * 100.0, 2)


def parse_card(card: Tag) -> dict | None:
    # extrai e normaliza todos os campos de um unico card html de produto
    el_titulo = card.select_one(selectors.TITULO)
    if not el_titulo:
        return None

    nome_produto = el_titulo.get_text(strip=True)
    url_original = str(el_titulo.get("href") or "").strip()
    id_produto = extrair_id_produto(url_original)
    url_limpa = limpar_url(url_original)

    el_img = card.select_one(selectors.IMAGEM)
    imagem_url = None
    if el_img:
        imagem_url = str(el_img.get("data-src") or el_img.get("src") or "").strip() or None

    el_orig_frac = card.select_one(selectors.PRECO_ORIGINAL_FRACAO)
    el_orig_cents = card.select_one(selectors.PRECO_ORIGINAL_CENTAVOS)
    valor_original = parse_preco(
        el_orig_frac.get_text(strip=True) if el_orig_frac else None,
        el_orig_cents.get_text(strip=True) if el_orig_cents else None,
    )

    el_atual_frac = card.select_one(selectors.PRECO_ATUAL_FRACAO)
    el_atual_cents = card.select_one(selectors.PRECO_ATUAL_CENTAVOS)
    valor_atual = parse_preco(
        el_atual_frac.get_text(strip=True) if el_atual_frac else None,
        el_atual_cents.get_text(strip=True) if el_atual_cents else None,
    )

    desconto_pct = calcular_desconto_pct(valor_original, valor_atual)

    el_nota = card.select_one(selectors.NOTA)
    el_vendas = card.select_one(selectors.VENDAS)
    texto_nota = el_nota.get_text(strip=True) if el_nota else None
    texto_vendas = el_vendas.get_text(strip=True) if el_vendas else None

    # se houver apenas um span de avaliacao contendo 'vendidos', trata como vendas sem nota
    if texto_nota and "vendid" in texto_nota.lower() and not texto_vendas:
        texto_vendas = texto_nota
        texto_nota = None

    nota = parse_nota(texto_nota)
    vendas = parse_vendas(texto_vendas)

    return {
        "id_produto": id_produto,
        "nome_produto": nome_produto,
        "url_original": url_original,
        "url_limpa": url_limpa,
        "imagem_url": imagem_url,
        "valor_original": valor_original,
        "valor_atual": valor_atual,
        "desconto_pct": desconto_pct,
        "nota": nota,
        "vendas": vendas,
    }


def parse_html_busca(html: str) -> list[dict]:
    # percorre todos os cards .poly-card da pagina de busca e retorna lista de dicionarios
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(selectors.CARD)
    itens = []
    for card in cards:
        parsed = parse_card(card)
        if parsed:
            itens.append(parsed)
    return itens
