# Stash — origem dos dados

Perfil: https://stash.games/users/MikeMequis1

Páginas:
- Jogos: /users/MikeMequis1/games/
- Reviews: /users/MikeMequis1/reviews/
- Review individual: /games/{slug}/reviews/MikeMequis1

As páginas do perfil são públicas; autenticação do app não implica autenticação para essas páginas.

## Acesso HTTP vs Browser (confirmado em 2026-11-23)
- HTTP `requests` com User-Agent de navegador: **HTTP 403**, página “Just a moment…” do Cloudflare, tanto em jogos quanto em reviews.
- Chromium Playwright **headful**: passou o desafio e carregou o perfil real.
- Chromium **headless**: permaneceu no desafio “Um momento…”.
- O export HTTP atual registra falha 403 no `export-report.json`; não inventa dados.
- O export browser reutiliza exatamente os mesmos parsers/coletores e gera os mesmos arquivos.

Perfil real observado: “Marselo II (@MikeMequis1)”, 1114 jogos, 76 reviews (contadores do perfil podem mudar).

## Paginação
- Estrutura `nav.profile-pagination` confirmada.
- Links reais usam `?page=N`; o coletor usa os hrefs da navegação.
- Jogos: **38 páginas**; reviews: **13 páginas**; 30 jogos e 6 reviews na primeira página.
- Não fixar totais: descobrir dinamicamente.

## Cards de jogos (seletores confirmados)
Seletor: `.games-list__item-link`.
- slug/URL: `href` (`/games/{slug}`)
- título: `.games-list__item-name` ou `data-text`
- data exibida: `.games-list__item-date` (ex.: “14 Oct 2008”)
- capa: `img[src]` ou `img[data-src]` (IGDB)
- status: primeiro ícone `<use href="#...">` (`#playing`, `#beaten`, `#want`, `#archived`, …)
- nota: `.game__rating` (texto numérico), não `[class*='rating']` genérico
- plataforma: não exposta no card; permaneceu ausente em 30/30 jogos da página 1
- ano: derivado da data exibida

`date_displayed` é preservada; seu significado exato (lançamento vs adição) não foi confirmado. Um jogo pode ter status e nota sem review textual.

## Cards de reviews (seletores confirmados)
Seletor: `.user__review-item`.
- link da review: `.user__review-comment[href]`
- título do jogo: `aria-label="View game: {título}"` em `.user__review-game`; fallback `span.font-semibold`
- texto: `.user__review-text` (listagem)
- data: `time.user__review-date[datetime]` (ISO)
- nota: `.user__review-rating`
- status: `img[src*='/images/statuses/']`, basename sem extensão (`playing`, `beaten`, `archived`, …)
- progresso: bloco com `use[href="#completionists"]` (ex.: “100% completion”)
- likes/comentários: `.user__review-likes-block` + ícone `#like`/`#comment`; contagens em `.user__review-likes`
- capa: último `img` válido em `.user__review-game` (ignorar `/images/statuses/`)

Armadilhas encontradas e corrigidas:
- primeiro `use` do card de review era a seta “Full review” (`#ar-right`), não status;
- primeiro `img` era o ícone de status, não a capa;
- `.user__review-game` inteiro concatenava título, nota e status; usar `aria-label`/span.

## Detalhe individual
- Seletor real do texto: `.game__review-comment` (não `.review__text`/`.user__review-text`).
- A listagem pode divergir do detalhe. Na página 1: 6 detalhes visitados, **2 divergências** registradas (`pseudoregalia`, `doki-doki-literature-club`).
- O texto integral do detalhe substitui o texto da listagem; o original fica em `raw.text_listing` e o detalhe em `raw.text_detail`.
- Texto vazio continua sendo review válida se houver nota.

## Campos efetivamente encontrados
Games: slug, título, URL, capa, ano, data exibida, status, nota (quando presente).
Reviews: slug, título, URL do jogo/review, capa, texto, nota, data, status, progresso (quando presente), likes, comentários.
Ausentes na página 1: plataforma nos jogos (30/30), nota em 15/30 jogos, progresso em 5/6 reviews.

## Coleta direcionada reproduzível (Etapa 1)
Comando real:

```powershell
python -m src.cli export --kind both --method browser `
  --slug demolition-girl --game-page 9 --review-page 4 `
  --run-id pilot-demolition-girl-20261124-v4
```

Comportamento:
- páginas explícitas não iniciam varredura pela página 1;
- slug filtra por igualdade exata, nunca por título;
- página acima da última detectada vira falha registrada;
- falha de HTTP/browser/parse vira `PageFailure`;
- resultado `directed` com exit 0 quando o escopo pedido foi concluído;
- `failed`/`target_not_found` com exit 2 quando não há evidência válida;
- arquivos em `data/stash/runs/<run-id>/` com manifesto SHA-256;
- `category` é extraída de `.games-list__category` (`Remaster`, `Mod`, `Expanded game`, `Port`, `Bundle` etc.) desde 2026-11-25;
- artefatos antigos em `data/migration/demolition-girl/` permanecem intactos.

Válidação real: run v4 leu 1 jogo na página 9 e 1 review na página 4; dados idênticos ao piloto anterior.

Coleta completa em 2026-11-25: `stage9-full-stash-browser-v2-20261125` leu 38/38 páginas de jogos (1.114 registros) e 13/13 de reviews (76 registros, 76 detalhes), com 11 divergências listagem/detalhe e campos ausentes registrados. Tentativas anteriores (`stage9-full-stash-20261125` HTTP 403 e `stage9-full-stash-browser-20261125` Cloudflare parcial) foram preservadas.

## Piloto Demolition Girl — campos confirmados
- Jogo na página 9; card: `20 May 2004`, status `archived`, nota 2.0, capa IGDB, plataforma ausente.
- Review na página 4; nota 2.0, data `2024-10-22T22:42:39Z`, status `archived`, likes 1, comentários 0, progresso ausente.
- Detalhe individual: texto integral idêntico à listagem, 239 caracteres.
- A reprodução via CLI em 2026-11-24 gerou dados idênticos em run versionado; ver `docs/execution-log.md`.

## Coleta
Requests/Playwright + BeautifulSoup; paginação dinâmica; jogos e reviews em coletas separadas; visitar reviews individuais; preservar HTML opcionalmente; deduplicar sem descartar colisões; gerar relatório de páginas, contagens, campos ausentes, divergências e falhas. Usar timeout, retry limitado, User-Agent e intervalo respeitoso.
