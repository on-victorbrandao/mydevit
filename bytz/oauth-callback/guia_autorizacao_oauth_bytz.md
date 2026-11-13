# OAuth da conta secundaria de busca

Preparado em **09/11/2026** para revisao central. Nenhum Worker, conta, redirect real ou autorizacao foi criado nesta entrega. O componente nao consulta produtos e nao participa da ponte S4.

## Etapa local entregue

Pronto quando: testes sinteticos e verificacoes do escopo passam, o diff esta separado da S4 e o revisor confere esta entrega. Publicar e autorizar de verdade e uma etapa posterior com o dono.

Fluxo: cliente Python local gera `state` e PKCE S256 → dono autoriza a conta secundaria no navegador → ML retorna ao Worker HTTPS → dono copia **a URL completa** para a entrada oculta do comando `concluir` → cliente valida e troca o codigo diretamente no ML. Nao existe servidor local aberto, tunel, redirecionamento para localhost, armazenamento remoto ou envio de segredo da aplicacao ao Worker.

O Worker aceita apenas GET na URI exata configurada e valida a estrutura do retorno. **Ele nao confirma que o estado pertence a uma tentativa**: essa verificacao e local, junto com PKCE, expiracao e uso unico. A pagina nunca ecoa parametros, nao tem JavaScript, formularios, recursos externos ou analytics. Todas as respostas levam `no-store`, `no-referrer`, CSP restritiva e bloqueio de iframe.

## Escolha gratuita e limites confirmados

- Workers Free: **100.000 requisicoes/dia por conta**, **10 ms de CPU por invocacao**. O receptor nao usa KV, D1, Durable Objects ou subrequisicoes. Custo novo zero depende de permanecer no Free, das cotas compartilhadas e dos termos vigentes; nao habilitar plano pago, cartao ou cobranca automatica. [Precos oficiais](https://developers.cloudflare.com/workers/platform/pricing/).
- A cota diaria reinicia a 00:00 UTC; excede-la pode devolver erro 1027. Pare e aguarde a disponibilidade, sem upgrade ou alternancia de servico. [Limites oficiais](https://developers.cloudflare.com/workers/platform/limits/).
- A conta Cloudflare e requisito. O subdominio `workers.dev` dispensa dominio proprio e a URL segue o nome do Worker e o subdominio da conta. Manter esses nomes preserva o endereco; nao usar URL de preview. O fornecedor recomenda dominio proprio para cargas criticas, portanto esta escolha e para autorizacao inicial de baixo volume, sem promessa de SLA. [workers.dev](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/).
- A documentacao ML exige HTTPS, redirect estatico identico ao cadastrado e estado validado pelo cliente. PKCE precisa estar habilitado na aplicacao e sera sempre S256 neste cliente. Isso **nao comprova aceitacao de workers.dev pela aplicacao real**. [Autorizacao ML](https://developers.mercadolivre.com.br/pt_br/mensagens-post-venda/autenticacao-e-autorizacao).
- Nenhum impedimento concreto ao Worker foi comprovado offline; ngrok nao foi introduzido. Se o cadastro recusar o endereco ou o provedor bloquear o retorno, parar e trazer a mensagem sanitizada para revisao. Nao contornar captcha ou bloqueio.

## Passagem externa pendente — somente depois do parecer central

Efeito da autorizacao desta passagem: criar/publicar **um receptor publico gratuito sem segredos**, registrar seu endereco na **aplicacao secundaria** e o dono conceder **leitura e acesso offline** a essa aplicacao. Nada muda na aplicacao principal. Estes passos ainda nao foram executados:

1. Dono entra no painel Cloudflare (ou cria a conta gratuita, se necessario) e confirma **Workers Free**. Se aparecer exigencia de pagamento, cartao, trial pago ou plano superior, parar. Em **Workers & Pages → Create application → Hello World**, usar o nome `bytz-search-oauth` e publicar o receptor inicial. Se o nome estiver indisponivel, ajustar somente esse nome e registrar o endereco exibido; nao adivinhar a URL. [Criacao pelo painel e Edit code](https://developers.cloudflare.com/workers/versions-and-deployments/gradual-deployments/).
2. Em **Settings → Domains & Routes**, copiar o endereco HTTPS de producao `workers.dev` exibido pelo painel e acrescentar `/oauth/callback`, sem barra final, query, fragmento ou porta. Este documento nao fornece uma URL final. Desativar preview URLs. Em **Settings → Observability**, desabilitar Workers Logs, traces e qualquer Logpush/exportacao; nao abrir Live Logs nem executar tail durante OAuth.
3. Em **Settings → Variables and Secrets**, adicionar **somente** `REDIRECT_URI` como texto com a URL completa do passo 2. Nao colocar Client Secret, token ou PKCE na Cloudflare. Em **Edit code**, substituir o Hello World pelo conteudo integral de `worker.mjs`, ajustar a data de compatibilidade para `2026-11-09` e publicar a versao completa, sem distribuicao gradual. O `wrangler.toml` registra a mesma configuracao para eventual uso futuro da CLI; o painel nao o importa automaticamente. Confirmar novamente logs/traces/previews desligados antes de OAuth.
4. Abrir a URL real sem parametros: deve aparecer retorno invalido (HTTP 400), sem redirecionar. Se aparecer Hello World, erro de configuracao ou desafio do provedor, parar e revisar. Essa checagem nao autoriza tentativa de contornar uma protecao.
5. No DevCenter do Mercado Livre, **na aplicacao secundaria de busca**, cadastrar exatamente a URL do passo 2 no campo **Redirect URI** e habilitar **PKCE**. Selecionar somente `read` e `offline_access`, sem escrita, operacoes financeiras ou Mercado Pago. Confirmar a conta titular secundaria e os requisitos que o DevCenter apresentar. Se exigir permissao adicional, parar para revisao; nao ampliar automaticamente.
6. Dono preenche privadamente `ML_SEARCH_CLIENT_ID`, `ML_SEARCH_CLIENT_SECRET`, `ML_SEARCH_REDIRECT_URI` e `ML_SEARCH_USER_ID` no `back/.env` existente. O ultimo campo e o ID numerico **previamente conferido da conta secundaria**, nao o ID da aplicacao. Se esse ID ainda nao estiver confirmado, essa e uma pendencia de identidade: nao inferir pelo primeiro token nem usar dados da principal. Nao colar esses valores no chat. O comando nao le cookies nem usa `SECRETS_DIR` para esta credencial.
7. Abrir o terminal na raiz do repositorio. Fechar sessoes da conta principal no navegador escolhido pelo dono e conferir visualmente que a autorizacao e da **secundaria**. Executar os comandos abaixo no terminal local. Antes da copia, desativar historico/sincronizacao da area de transferencia do Windows e evitar gerenciadores de clipboard, extensoes que capturem URLs, gravacao de tela e terminal compartilhado.

```powershell
cd .\bytz\back
.\.venv\Scripts\python.exe -m oauth_busca iniciar
# O dono confere a conta secundaria e autoriza no navegador.
.\.venv\Scripts\python.exe -m oauth_busca concluir
```

8. Na pagina de retorno, copiar a URL da barra de enderecos e colar **somente na entrada oculta desse terminal**, imediatamente. O shell nao recebe a URL como argumento nem grava esse valor no historico de comandos. O terminal precisa ser interativo e suportar entrada oculta; caso contrario o comando falha. Nao criar arquivo com o retorno, nem mandar codigo/token/URL ao chat. Ao concluir, fechar a aba, limpar clipboard/historico do clipboard e remover esse retorno do historico local do navegador. A mensagem esperada e `Autorizacao da conta secundaria salva com protecao local.`

Pronto quando externo: URL real responde como previsto, DevCenter aceita a URI, autorizacao ocorre na conta secundaria e cliente confirma identidade/permissoes. Nao houve essa prova nesta entrega. A proxima tarefa de consulta a produto depende de novo parecer central.

## Persistencia, renovacao e falhas

Estado, PKCE e tokens ficam juntos em `back/secrets/ml-search-oauth/vault.dpapi`, ignorado pelo Git, separado de cookies e do aplicativo principal. O arquivo e cifrado por **DPAPI CurrentUser**, sem chave no codigo; o temporario tambem e cifrado. `fsync` precede `os.replace` no mesmo diretorio. DPAPI protege os dados em repouso para a identidade Windows, mas processos executados por esse usuario podem decifra-los; nao protege uma sessao Windows comprometida. [Microsoft DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).

Preservada a decisao do projeto de manter segredos dentro do projeto/OneDrive privado. O `.env` continua privado e contem o Client Secret; DPAPI nao cifra o `.env`. Nao compartilhar a pasta, restaurar cofre antigo, editar seu conteudo, abrir duas copias sincronizadas ou executar de outro computador. A trava e local, nao distribuida; mudanca de usuario/maquina e recuperacao de backup exigem revisao e possivelmente nova autorizacao. Nao mexer em permissoes das pastas existentes nem duplicar segredos para a Cloudflare.

A tentativa local dura **5 minutos desde iniciar**; `iniciar` explicito substitui a tentativa pendente. O instante de emissao do codigo nao vem no retorno, portanto o cliente nao consegue medir sua idade exata: o ML pode rejeita-lo antes desse limite. Colar imediatamente; uma rejeicao de codigo expirado/usado nao dispara retry. Estados divergentes nao consomem a tentativa legitima. Retorno vinculado consome a tentativa antes da rede, inclusive em negacao, falta de codigo ou falha. Hashes dos ultimos 100 codigos trocados impedem repeticao local adicional; o servidor continua responsavel pela validade do codigo.

O access token respeita `expires_in`, com margem de 60 segundos. O refresh mais recente e usado uma vez, sob trava de arquivo entre processos. O cofre e marcado `refresh_pending` e perde a copia utilizavel **antes** do POST. A nova dupla substitui a antiga atomicamente. A documentacao descreve tokens de acesso de 6h e refresh rotativo de uso unico; nao se fixa esse prazo no cliente. [Guia oficial ML](https://developers.mercadolivre.com.br/pt_br/tendencias/desenvolvimento-in-house).

```powershell
# Somente depois da autorizacao real aprovada; nao exibe o token.
.\.venv\Scripts\python.exe -m oauth_busca renovar
```

Este comando reutiliza o access token vigente ou renova se expirou/esta na margem. Reutilizar nao faz chamada e nao comprova que uma revogacao antecipada nao ocorreu. Nao existe agendamento nem integracao com coleta. HTTP 400/401 (incluindo invalid_grant/revogacao), 403/429, timeout, resposta invalida ou falha de persistencia interrompem a operacao; nenhum retry automatico e nenhuma alternancia de conta. Falha de refresh deixa o cofre pendente, sem reusar o refresh antigo. Perder a resposta apos o POST pode exigir nova autorizacao manual: preferiu-se essa parada a duplicar um refresh de uso unico. Em 403/429/captcha/verificacao de conta, parar e avisar; nao executar novo iniciar para continuar uma operacao bloqueada. Uma futura integracao devera parar no 401 de recurso, sem ciclo automatico de login; ela nao faz parte desta entrega. Sem chamada automatica para revogar; o dono pode remover a permissao dessa aplicacao secundaria no ML quando necessario.

O POST e fixo em `api.mercadolibre.com/oauth/token`, com TLS validado, timeout de 20s, sem redirects, proxy herdado ou logs de payload. Credenciais recebidas de outra conta ou scopes alem de leitura/offline sao rejeitadas e nao persistidas; se a concessao real tiver ocorrido com permissoes erradas, o dono deve remove-la no ML antes de nova tentativa. O codigo nao altera configuracao/permissoes do DevCenter.

## Logs e transporte manual

O codigo nao usa logger para dados OAuth: mensagens sao constantes, erros externos nao sao impressos, e a URL de autorizacao e aberta diretamente no navegador. `core/logger.py` foi inspecionado, mas seu sanitizador atual nao cobre todos os campos OAuth; por isso nao se encaminha a ele URL, excecao bruta ou payload. Essa entrega nao altera o logger compartilhado.

Na Cloudflare, invocation logs podem incluir metodo e URL. Manter `observability.enabled=false`, logs/traces desligados e `invocation_logs=false`; nao anexar Tail Worker, Logpush, analytics de terceiros nem proxies de observacao. No painel, aplicar manualmente as mesmas restricoes antes da autorizacao e verifica-las depois de publicar. Nao enviar retorno OAuth para ferramentas de diagnostico. [Workers Logs](https://developers.cloudflare.com/workers/observability/logs/workers-logs/).

TLS protege navegador→Worker e cliente→ML; a passagem Worker→cliente e uma copia manual local, sem servidor intermediario de transporte. O provedor HTTPS necessariamente recebe a query, e o navegador/clipboard a mantem temporariamente; desligar os logs controlaveis nao promete ausencia de processamento interno de seguranca do fornecedor. O receptor nunca recebe o verificador PKCE, Client Secret, access token ou refresh token. Esta limitacao do transporte manual e explicita e deve ser considerada na revisao central.

## Validacao offline

Python 3.11+ (execucao verificada em 3.13), Windows/DPAPI, pytest/Ruff/Black ja instalados e Node 24. Nenhuma dependencia adicionada ou instalada. Testes exercitam o modulo Worker real com Request/Response nativos do Node; nao comprovam o runtime Cloudflare, o painel ou a aceitacao da URI pelo ML. Wrangler, ESLint e Prettier nao foram instalados por esta tarefa; sintaxe JS e testes nativos sao verificacoes distintas de lint JS. Execute os comandos a partir da raiz do repositorio.

```powershell
cd .\bytz\back
$env:PYTHON_DOTENV_DISABLED='1'
.\.venv\Scripts\python.exe -m pytest tests/test_oauth_busca.py -q --tb=short
.\.venv\Scripts\python.exe -m ruff check oauth_busca tests/test_oauth_busca.py
.\.venv\Scripts\python.exe -m black --check --workers 1 oauth_busca tests/test_oauth_busca.py
cd ..\oauth-callback
node --check worker.mjs
node --check worker.test.mjs
node --test worker.test.mjs
```

Se a pasta temporaria padrao do pytest nao for gravavel, usar `--basetemp` com uma **pasta nova, vazia e exclusiva de testes** dentro de `back/.pytest_cache/`. O pytest pode apagar essa pasta em reexecucoes: nunca apontar para pasta de segredos ou de trabalho. Nao rodar o doctor real nem a suite geral nesta validacao isolada.
