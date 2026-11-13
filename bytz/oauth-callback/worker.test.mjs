import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import worker from "./worker.mjs";

// Todos os enderecos e retornos destes testes sao sinteticos.
const uri = "https://oauth.exemplo.workers.dev/oauth/callback";
const state = "s".repeat(43);
const env = { REDIRECT_URI: uri };
const query = `?code=CODIGO-FICTICIO&state=${state}`;
const run = (suffix = query, config = env, method = "GET") =>
  worker.fetch(new Request(uri + suffix, { method }), config);

test("retorno valido sem eco, cache, scripts, links ou redirecionamento", async () => {
  const response = await run();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.ok(!html.includes(state));
  assert.ok(!html.includes("CODIGO-FICTICIO"));
  assert.ok(!/<script|<iframe|<form|<a |<img/i.test(html));
  assert.equal(response.headers.get("Location"), null);
  assert.match(response.headers.get("Cache-Control"), /no-store/);
  assert.equal(response.headers.get("Referrer-Policy"), "no-referrer");
  assert.match(response.headers.get("Content-Security-Policy"), /default-src 'none'/);
});

test("erros e parametros maliciosos nunca entram no HTML", async () => {
  const response = await run(`?error=access_denied&state=${state}&error_description=%3Cscript%3Eataque%3C/script%3E`);
  assert.equal(response.status, 200);
  assert.ok(!(await response.text()).includes("ataque"));
});

for (const suffix of [
  "", "?code=x", `?code=x&state=errado`, `?state=${state}`,
  query + "&code=outro", query + "&state=outro", query + "&next=https://outro.invalid",
  query + "&error=access_denied", `?code=&state=${state}`,
  `?code=%3Cscript%3E&state=${state}`, `?error=&state=${state}`,
  query + "&error_description=" + "x".repeat(8192),
]) {
  test(`rejeita retorno sintetico invalido ${suffix.slice(0, 24)}`, async () => {
    assert.equal((await run(suffix)).status, 400);
  });
}

test("URI divergente, HTTP, rota extra e metodo indevido falham", async () => {
  for (const url of [uri.replace("https:", "http:"), uri.replace("oauth.", "outro."), uri + "/extra"]) {
    assert.equal((await worker.fetch(new Request(url + query), env)).status, 404);
  }
  assert.equal((await run(query, env, "POST")).status, 405);
});

test("configuracao ausente ou dinamica nao habilita o receptor", async () => {
  for (const REDIRECT_URI of [undefined, "", uri + "?x=1", uri + "#x", "http://local.invalid/oauth/callback"]) {
    assert.equal((await run(query, { REDIRECT_URI })).status, 503);
  }
});

test("configuracao versionada desliga logs, traces e previews", () => {
  const config = readFileSync(new URL("./wrangler.toml", import.meta.url), "utf8");
  assert.match(config, /\[observability\]\s+enabled = false/);
  assert.match(config, /invocation_logs = false/);
  assert.match(config, /preview_urls = false/);
  assert.ok(!/console\.|fetch\(/.test(readFileSync(new URL("./worker.mjs", import.meta.url), "utf8").replace("async fetch(", "async handler(")));
});
