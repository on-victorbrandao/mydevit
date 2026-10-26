# testes do modulo de geracao de links de afiliado individual e lote
import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from activation.affiliate import (
    AffiliateError,
    executar_estagio_ativacao,
    executar_requisicao_create_link,
    executar_requisicao_linkbuilder,
    extrair_links_resposta,
    gerar_links_afiliado,
    montar_payload,
    validar_url_produto,
)
from core.db import init_db


def test_validar_url_produto():
    # valida se o filtro de url rejeita bugs do legado e aceita links validos do mercado livre
    assert (
        validar_url_produto(
            "https://www.mercadolivre.com.br/carregador-portatil-20000mah-com-carregamento-rapido-geonav-cinza/p/MLB67407121"
        )
        is True
    )
    assert validar_url_produto("https://mercadolivre.com.br/p/MLB123") is True

    # rejeita url com = no inicio (bug legado do n8n)
    assert (
        validar_url_produto("=https://www.mercadolivre.com.br/power-bank-pro-20000mah/p/MLB21524545") is False
    )
    # rejeita links de publicidade mclics
    assert validar_url_produto("https://www.mercadolivre.com.br/mclics/click_tracking_id=123") is False
    # rejeita links de outros dominios ou esquemas inseguros
    assert validar_url_produto("http://www.mercadolivre.com.br/p/MLB123") is False
    assert validar_url_produto("https://externo.com/produto") is False
    assert validar_url_produto("") is False


def test_montar_payload():
    # valida construcao do payload para createLink
    urls = [
        "https://www.mercadolivre.com.br/p/MLB1",
        "https://www.mercadolivre.com.br/p/MLB2",
    ]
    payload = montar_payload(urls, tag="bytz_campanha")
    assert payload["urls"] == urls
    assert payload["tag"] == "bytz_campanha"


def test_extrair_links_resposta_explicita():
    # valida extracao quando a api devolve original_url em cada objeto
    resposta = {
        "status": 200,
        "urls": [
            {
                "original_url": "https://www.mercadolivre.com.br/p/MLB1",
                "short_url": "https://mercadolivre.com/sec/mlb1",
                "status": 200,
            },
            {
                "original_url": "https://www.mercadolivre.com.br/p/MLB2",
                "short_url": "https://mercadolivre.com/sec/mlb2",
                "status": 200,
            },
        ],
    }
    solicitadas = [
        "https://www.mercadolivre.com.br/p/MLB1",
        "https://www.mercadolivre.com.br/p/MLB2",
    ]
    res = extrair_links_resposta(resposta, solicitadas)
    assert len(res["sucessos"]) == 2
    assert res["mapa"]["https://www.mercadolivre.com.br/p/MLB1"] == "https://mercadolivre.com/sec/mlb1"
    assert res["mapa"]["https://www.mercadolivre.com.br/p/MLB2"] == "https://mercadolivre.com/sec/mlb2"
    assert len(res["falhas"]) == 0


def test_extrair_links_resposta_posicional_e_falha_parcial():
    # valida correspondencia posicional quando a resposta nao repete a url original
    resposta = {
        "status": 200,
        "urls": [
            {"short_url": "https://mercadolivre.com/sec/mlb1", "status": 200},
            {"short_url": "", "status": 400, "error": "Produto indisponivel"},
        ],
    }
    solicitadas = [
        "https://www.mercadolivre.com.br/p/MLB1",
        "https://www.mercadolivre.com.br/p/MLB2",
    ]
    res = extrair_links_resposta(resposta, solicitadas)
    assert len(res["sucessos"]) == 1
    assert res["mapa"]["https://www.mercadolivre.com.br/p/MLB1"] == "https://mercadolivre.com/sec/mlb1"
    assert len(res["falhas"]) == 1
    assert res["falhas"][0]["url_original"] == "https://www.mercadolivre.com.br/p/MLB2"


def test_executar_requisicao_linkbuilder_bloqueio():
    # simula redirecionamento para tela de login caracterizando sessao expirada
    mock_resp = MagicMock()
    mock_resp.status_code = 302
    mock_resp.headers = {"Location": "https://www.mercadolivre.com.br/login"}

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    with pytest.raises(AffiliateError) as exc_info:
        executar_requisicao_linkbuilder("cookie_fake", session=mock_session)

    assert exc_info.value.bloqueio is True
    assert "Sessao expirada" in str(exc_info.value)


def test_executar_requisicao_create_link_body_invalido():
    # simula status 200 http mas body.status 400 da api do mercado livre
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": 400, "message": "tag invalida"}
    mock_resp.raw.headers = MagicMock()
    mock_resp.raw.headers.getlist.return_value = []

    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    with pytest.raises(AffiliateError) as exc_info:
        executar_requisicao_create_link(["https://url"], "cookie", "csrf", session=mock_session)

    assert exc_info.value.body_status == 400


def test_gerar_links_afiliado_lote_mockado(tmp_path):
    # cria arquivo de cookie valido para simular o fluxo completo
    cookie_file = tmp_path / "ml_cookies.json"
    dados = {
        "marketplace": "Mercado Livre",
        "origem": "linkbuilder",
        "atualizacao": "2026-10-23T20:00:00-03:00",
        "cookie": "ssid=valid_ssid; _csrf=valid_csrf; nsa_rotok=tok1",
    }
    cookie_file.write_text(json.dumps(dados), encoding="utf-8")

    mock_session = MagicMock()
    # mock do linkbuilder GET
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.raw.headers.getlist.return_value = ["_csrf=csrf_novo; Path=/"]
    mock_session.get.return_value = mock_get_resp

    # mock do createLink POST
    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 200
    mock_post_resp.json.return_value = {
        "status": 200,
        "urls": [
            {
                "original_url": "https://www.mercadolivre.com.br/p/MLB1",
                "short_url": "https://mercadolivre.com/sec/mlb1",
                "status": 200,
            },
            {
                "original_url": "https://www.mercadolivre.com.br/p/MLB2",
                "short_url": "https://mercadolivre.com/sec/mlb2",
                "status": 200,
            },
        ],
    }
    mock_post_resp.raw.headers.getlist.return_value = []
    mock_session.post.return_value = mock_post_resp

    urls = [
        "https://www.mercadolivre.com.br/p/MLB1",
        "https://www.mercadolivre.com.br/p/MLB2",
    ]
    res = gerar_links_afiliado(
        urls,
        modalidade="lote",
        cookie_path=cookie_file,
        session=mock_session,
    )

    assert len(res["sucessos"]) == 2
    assert res["mapa"]["https://www.mercadolivre.com.br/p/MLB1"] == "https://mercadolivre.com/sec/mlb1"
    assert res["mapa"]["https://www.mercadolivre.com.br/p/MLB2"] == "https://mercadolivre.com/sec/mlb2"

    # confere se o cookie foi atualizado com csrf_novo no arquivo
    lido = json.loads(cookie_file.read_text(encoding="utf-8"))
    assert "_csrf=csrf_novo" in lido["cookie"]


def test_executar_estagio_ativacao_completo(tmp_path):
    # configura banco temporario e arquivo de cookie para testar a pipeline completa
    db_file = tmp_path / "test_stage.db"
    init_db(db_file)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row

    conn.execute("""
        INSERT INTO produtos (id_produto, nome_produto, url_original, status, score)
        VALUES
            ('MLB10', 'Power Bank 1', 'https://www.mercadolivre.com.br/p/MLB10', 'curado', 92.0),
            ('MLB20', 'Power Bank 2', 'https://www.mercadolivre.com.br/p/MLB20', 'curado', 85.0)
        """)
    conn.commit()

    cookie_file = tmp_path / "ml_cookies.json"
    dados = {
        "marketplace": "Mercado Livre",
        "origem": "linkbuilder",
        "atualizacao": "2026-10-23T20:00:00-03:00",
        "cookie": "ssid=valid_ssid; _csrf=valid_csrf; nsa_rotok=tok1",
    }
    cookie_file.write_text(json.dumps(dados), encoding="utf-8")

    mock_session = MagicMock()
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.raw.headers.getlist.return_value = []
    mock_session.get.return_value = mock_get_resp

    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 200
    mock_post_resp.json.return_value = {
        "status": 200,
        "urls": [
            {
                "original_url": "https://www.mercadolivre.com.br/p/MLB10",
                "short_url": "https://mercadolivre.com/sec/mlb10",
                "status": 200,
            },
            {
                "original_url": "https://www.mercadolivre.com.br/p/MLB20",
                "short_url": "https://mercadolivre.com/sec/mlb20",
                "status": 200,
            },
        ],
    }
    mock_post_resp.raw.headers.getlist.return_value = []
    mock_session.post.return_value = mock_post_resp

    res = executar_estagio_ativacao(
        modalidade="lote",
        conn=conn,
        session=mock_session,
        cookie_path=cookie_file,
    )

    assert res["processados"] == 2
    assert res["sucessos"] == 2
    assert res["falhas"] == 0

    # valida promocao no banco
    p1 = conn.execute("SELECT status, url_afiliado FROM produtos WHERE id_produto = 'MLB10'").fetchone()
    assert p1["status"] == "link_pronto"
    assert p1["url_afiliado"] == "https://mercadolivre.com/sec/mlb10"

    # valida registro na tabela execucoes
    execucao = conn.execute("SELECT * FROM execucoes WHERE modulo = 'activation'").fetchone()
    assert execucao is not None
    assert execucao["status"] == "sucesso"
    assert execucao["itens"] == 2
    conn.close()
