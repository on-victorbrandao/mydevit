// O receptor nao armazena, registra ou troca codigos por tokens.
const headers = {
  "Content-Type": "text/html; charset=utf-8",
  "Cache-Control": "no-store, max-age=0",
  Pragma: "no-cache",
  "Referrer-Policy": "no-referrer",
  "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Strict-Transport-Security": "max-age=31536000",
};

function page(message, status) {
  // Apenas textos constantes chegam ao HTML, sem eco da query ou scripts.
  return new Response(
    `<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>BYTZ — retorno OAuth</title><h1>Retorno da conta de busca</h1><p>${message}</p>`,
    { status, headers },
  );
}

export default {
  async fetch(request, env) {
    // Sem URL real configurada, falha fechado e nao recebe autorizacoes.
    let expected;
    try {
      expected = new URL(env.REDIRECT_URI);
      if (
        expected.protocol !== "https:" || expected.pathname !== "/oauth/callback" ||
        expected.search || expected.hash || expected.username || expected.password || expected.port ||
        !/^[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev$/.test(expected.hostname) ||
        expected.href !== env.REDIRECT_URI
      ) throw new Error();
    } catch {
      return page("Receptor ainda nao configurado.", 503);
    }
    const url = new URL(request.url);
    if (url.origin !== expected.origin || url.pathname !== expected.pathname || url.hash) {
      return page("Endereco de retorno invalido.", 404);
    }
    if (request.method !== "GET") {
      return page("Metodo nao permitido.", 405);
    }
    // A vinculacao criptografica e o uso unico sao validados exclusivamente no cliente local.
    const params = url.searchParams;
    const allowed = new Set(["code", "state", "error", "error_description", "error_uri"]);
    if (
      request.url.length > 8192 || [...params.keys()].some((key) => !allowed.has(key) || params.getAll(key).length !== 1) ||
      !/^[A-Za-z0-9_-]{43}$/.test(params.get("state") ?? "") ||
      params.has("code") === params.has("error") ||
      (params.has("code") && !/^[A-Za-z0-9._~-]{1,2048}$/.test(params.get("code"))) ||
      (params.has("error") && !/^[A-Za-z0-9_]{1,128}$/.test(params.get("error")))
    ) {
      return page("Retorno incompleto ou invalido. Nao compartilhe esta URL.", 400);
    }
    return page(
      "Volte ao terminal local e execute a etapa concluir. Copie a URL completa da barra de enderecos e cole somente na entrada oculta do terminal. Nao cole no chat, nao tire captura e nao compartilhe. O cliente local ainda precisa validar este retorno. Depois feche esta aba e limpe a area de transferencia.",
      200,
    );
  },
};
