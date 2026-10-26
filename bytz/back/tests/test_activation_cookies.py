# testes unitarios do modulo de cookies da ativacao
import json

import pytest

from activation.cookies import (
    CookieError,
    carregar_cookie,
    merge_cookie,
    parse_cookie_string,
    salvar_cookie_atomico,
    validar_cookie_conteudo,
)


def test_parse_cookie_string_basico():
    # valida parsing de string simples e extracao correta de chaves e valores
    raw = "ssid=abc12345; _csrf=csrf987; nsa_rotok=token_xyz"
    jar = parse_cookie_string(raw)
    assert jar["ssid"] == "abc12345"
    assert jar["_csrf"] == "csrf987"
    assert jar["nsa_rotok"] == "token_xyz"


def test_parse_cookie_string_duplicatas_mantem_ultimo():
    # garante que a ultima ocorrencia de um cookie repetido sobreponha a anterior
    raw = "tooltips-configuration=old; ssid=123; tooltips-configuration=new"
    jar = parse_cookie_string(raw)
    assert jar["tooltips-configuration"] == "new"
    assert jar["ssid"] == "123"


def test_merge_cookie_multiplos_set_cookie():
    # valida merge de cookies preservando chaves antigas e atualizando com novos cabecalhos
    cookie_antigo = "ssid=antigo; _csrf=antigo_csrf; nsa_rotok=tok1"
    set_cookies = [
        "ssid=novo_ssid; Path=/; Domain=.mercadolivre.com.br; Secure",
        "_csrf=novo_csrf; Path=/; HttpOnly",
        "novo_cookie=valor123; Path=/",
    ]
    atualizado, csrf = merge_cookie(cookie_antigo, set_cookies)
    jar = parse_cookie_string(atualizado)

    assert jar["ssid"] == "novo_ssid"
    assert jar["_csrf"] == "novo_csrf"
    assert jar["nsa_rotok"] == "tok1"
    assert jar["novo_cookie"] == "valor123"
    assert csrf == "novo_csrf"


def test_validar_cookie_conteudo_obrigatorios():
    # confere que a presenca dos 3 obrigatorios retorna sucesso
    valido_str = "ssid=1; _csrf=2; nsa_rotok=3; extra=4"
    valido, faltando = validar_cookie_conteudo(valido_str)
    assert valido is True
    assert faltando == []

    invalido_str = "ssid=1; extra=4"
    valido, faltando = validar_cookie_conteudo(invalido_str)
    assert valido is False
    assert "_csrf" in faltando
    assert "nsa_rotok" in faltando


def test_carregar_cookie_arquivo_inexistente(tmp_path):
    # arquivo inexistente deve levantar CookieError sem vazar paths criticos
    caminho = tmp_path / "inexistente.json"
    with pytest.raises(CookieError, match="Arquivo de cookie de afiliado nao encontrado"):
        carregar_cookie(caminho)


def test_carregar_cookie_json_invalido(tmp_path):
    # arquivo com conteudo corrompido deve levantar CookieError
    caminho = tmp_path / "quebrado.json"
    caminho.write_text("{corrompido", encoding="utf-8")
    with pytest.raises(CookieError, match="Falha ao ler JSON"):
        carregar_cookie(caminho)


def test_carregar_cookie_obrigatorios_ausentes(tmp_path):
    # json sem os cookies obrigatorios deve falhar com lista de faltando
    caminho = tmp_path / "incompleto.json"
    dados = {
        "marketplace": "Mercado Livre",
        "origem": "linkbuilder",
        "atualizacao": "2026-10-23T10:00:00-03:00",
        "cookie": "ssid=apenas_ssid",
    }
    caminho.write_text(json.dumps(dados), encoding="utf-8")
    with pytest.raises(CookieError, match="Cookies obrigatorios ausentes"):
        carregar_cookie(caminho)


def test_salvar_cookie_atomico(tmp_path):
    # valida gravacao atomica garantindo arquivo valido no disco
    caminho = tmp_path / "ml_cookies.json"
    dados = {
        "marketplace": "Mercado Livre",
        "origem": "linkbuilder",
        "atualizacao": "2026-10-23T20:00:00-03:00",
        "cookie": "ssid=1; _csrf=2; nsa_rotok=3",
    }
    salvar_cookie_atomico(dados, caminho)
    assert caminho.exists()

    lido = carregar_cookie(caminho)
    assert lido["cookie"] == dados["cookie"]
    assert lido["atualizacao"] == dados["atualizacao"]
