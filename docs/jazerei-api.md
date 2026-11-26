# Já Zerei — API e interface observadas

Observações empíricas de DevTools e Playwright. Não é documentação oficial. Separar o que foi **confirmado em teste real** do que continua **hipótese**.

## Confirmado em 2026-11-23

### Busca de catálogo (pública)
`POST https://jazerei.com/api/search/games` com `{"q":"Sonic Unleashed","limit":3}` retornou **HTTP 200 sem autenticação**, corpo JSON array.

Campos observados:
- `id` (IGDB), `slug`, `name`
- `cover` objeto (`id`, `image_id`) — não string; converter para `https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{image_id}.jpg`
- `first_release_date` epoch Unix (ex.: 1227139200 → 2008)
- `genres`, `platforms` listas de objetos com `name`
- `involved_companies` com `company.name` e `developer`
- `rating` IGDB 0–100 (não é a nota do usuário)

Resultado “Sonic Unleashed” incluiu ID 5169 e variantes (`sonic-unleashed-plus`, `sonic-unleashed-3ds`): não escolher automaticamente o primeiro.

HTTP e browser (fetch same-origin) produziram modelos idênticos. A busca é o único POST público liberado além de metadados.

### Página de detalhe
- Rota real: `/jogos/{slug}` (confirmada).
- `/game/{slug}`, `/jogo/{slug}`, `/games/{slug}` caem em “Essa página não existe”.
- DOM contém JSON-LD `VideoGame` com `name`, `url`, `description`, `datePublished`, `genre`, `image`.
- `fetch_game_detail` extrai `h1`, meta tags e JSON-LD; não depende de API privada.

### Requisições de leitura observadas
- `POST /api/igdb/query` — metadados de catálogo; liberado na política.
- `POST https://{projeto}.supabase.co/rest/v1/rpc/public_get_top_reviewers` — RPC pública de leitura; liberada.
- Assets e páginas GET.

### Sessão
- Login Google manual **confirmado**: `logged_in: true`, `login_visible: false`, marcador `firebase:authUser` no IndexedDB.
- Perfil persistente guarda cookies/localStorage; IndexedDB vazio não conta como login.
- Rota do perfil autenticado: `/u/{username}`; abas `?tab=atividade|jogos|avaliações|quero jogar|platinas|rankings|curtidas|streakmap`.
- `browser status --verify` após login: 125 requisições, 0 bloqueadas.
- `browser reviews`: captura de rede de `/rest/v1/reviews`; conta retornou 1 review.
- No login Google, 18 requisições de telemetria ficaram bloqueadas (`/api/sinal`, `play.google.com/log`, `signaler-pa.googleapis.com`); não impediram a autenticação. `public_user_id_exists` foi classificada como leitura e liberada.
- Google pode bloquear navegador automatizado em outros ambientes; alternativa é e-mail/senha quando disponível.

### Estado do piloto Demolition Girl
- Catálogo público: único candidato ID 26570 (`demolition-girl`), 2004, Tamsoft, PS2.
- Página autenticada `/jogos/demolition-girl`: 4 respostas de `/rest/v1/reviews`, todas vazias; nenhuma review da conta para 26570.
- Interface mostrou “Avaliar” e não “Editar”, consistente com ausência de review existente.
- Nenhuma operação de escrita foi executada.

### `liked` — evidência atual
- `JogoDetalhe` usa o rótulo “Recomendar este jogo”/“Recomendado” e envia `{game_id, rating, text, liked}` para `/api/reviews/upsert`.
- O formulário inicia `liked:false`; ao editar, carrega `c.liked ?? false`.
- GET autenticado retornou `liked:false` persistido na review existente.
- Curtidas de review são outra tabela (`review_likes`), com chave `${uid}_${reviewId}`; não são representadas por `liked`.
- Portanto, `liked` = recomendação do autor, não curtida da review.
- Não há prova sobre omissão/`null` no endpoint; não testar com escrita.
- O Stash não tem equivalente observado; migração não deve inventar valor.

### Etapa 2 implementada (review-state)
- Identidade: UID local (IndexedDB Firebase), `get_my_profile.id` e a identidade esperada (`JAZEREI_USER_ID` ou UID salvo no login) devem coincidir.
- Paginação: GET `/rest/v1/reviews` com `Range`/`Range-Unit`; `Content-Range` quando disponível.
- Critérios de término: `content_range_total`, `short_page` ou `empty_page`; página repetida/erro/total não atingido invalidam.
- Deduplicação por `id`; `user_id` de cada registro é validado.
- Snapshot em `data/jazerei/runs/<run-id>/review-state/<slug>-<game-id>.json` + manifesto.
- Snapshot completo da conta em `data/jazerei/runs/<run-id>/review-state/account-reviews.json` + manifesto (`kind: jazerei-account-review-state`), capturado por `browser account-reviews`.
- `exists:false` só é publicado após consulta completa; falha gera `capture-failure.json`.
- Campos sensíveis em URLs (ex.: `user_avatar?token=...`) e dados pessoais desnecessários (avatar, e-mail, nome, telefone) são descartados.
- `liked` é preservado como recomendação do autor; ver política abaixo.

## `liked` — contrato, equivalência e política

### Fatos comprovados
- `liked` é a recomendação do autor sobre o jogo, persistida na linha da review.
- O frontend envia `{game_id, rating, text, liked}`; o formulário inicia `false` e alterna entre `true`/`false`.
- GET autenticado retorna `liked` persistido (exemplo real: `false`).
- Curtidas de review são `review_likes`/`likes_count`; comentários são `comments_count`. Nenhum é `liked`.
- O Stash expõe rating, texto, status, progresso, likes e comentários, mas **nenhum campo de recomendação equivalente confirmado**.

### Contrato do backend — parcialmente desconhecido
| Pergunta | Evidência | Conclusão |
|---|---|---|
| `liked` é obrigatório? | frontend sempre envia; gateway exige | não provado no servidor |
| aceita `true`/`false`? | uso do frontend e linha persistida | frontend usa; backend não testado por escrita |
| aceita `null`? | nenhuma evidência | desconhecido |
| aceita omissão? | nenhuma evidência | desconhecido |
| default no banco? | linha existente `false`; formulário inicia `false` | hipótese `false`, não contrato |
| validação/transformação? | nenhum código de backend disponível | desconhecido |
| frontend = backend? | não verificado | não presumir |

Nenhum POST/PATCH/PUT/DELETE foi enviado para descobrir o contrato. `GET /rest/v1/` (OpenAPI) retornou **401 Secret API key required** com a chave da sessão pública; o schema do banco não está acessível por esse caminho. Portanto, default, nulabilidade e validação do backend permanecem desconhecidos.

### Política adotada
- A política automática solicitada em 2026-11-25 usa a nota: `≥7` true, `≤5` false, entre 5 e 7 unresolved; decisões automáticas usam `source: automatic`.
- Decisões humanas têm precedência e não são sobrescritas automaticamente.
- Sem equivalente no Stash, o valor final continua vinculado a uma decisão registrada por review.
- Decisão vinculada a `source_ref`, `game_id` e `source_hash` da review.
- Decisão inválida/desatualizada não é reutilizada; estado fica `stale`.
- Review existente no destino é preservada, incluindo `liked`; nenhuma atualização é proposta.
- Enquanto não resolvido, o planner bloqueia a ação (`liked_unresolved`).
- O `null` interno de `LikedState` não afirma que o endpoint aceita `null`.

### Registro de decisões
- Arquivo append-only: `data/migration/liked-decisions.jsonl`.
- CLI:
  ```powershell
  python -m src.cli migration liked decide --source-ref demolition-girl --game-id 26570 --value false --review-file <reviews-run>.json
  python -m src.cli migration liked list
  python -m src.cli migration liked show --source-ref demolition-girl --game-id 26570 --review-file <reviews-run>.json
  ```
- `--replace` cria nova revisão e preserva a anterior via `supersedes`.
- Decisão para jogo diferente não se aplica.

## Documentado anteriormente (não reconfirmado nesta rodada)

### Autenticação
Firebase Secure Token observado em `https://securetoken.googleapis.com/v1/token?key=REDACTED`. Resposta inclui access_token, refresh_token, id_token, expires_in e user_id. Cabeçalho exigido por endpoint ainda não confirmado. Nunca versionar/logar tokens; usar sessão legítima do perfil persistente.

### Reviews
Leitura: `GET /rest/v1/reviews?select=*&user_id=eq.{USER_ID}&order=created_at.desc`.
Campos: id, game_id, game_slug, game_name, game_cover, game_year, developer, user_id, username, rating, text, created_at, likes_count, comments_count, short_id, genres, moderation_status. Review com texto vazio é válida.

Upsert (bloqueado por padrão): `POST /api/reviews/upsert`, `{"game_id":26570,"rating":0.5,"text":"ruim","liked":false}`. O único transporte está no gateway, atrás de dois gates e sem uso real até agora.

O frontend busca o token com `currentUser.getIdToken(true)` e envia apenas `Content-Type: application/json` e `Authorization: Bearer <ID token Firebase>` para `/api/reviews/upsert`. Sem esse cabeçalho, o endpoint responde `401 Authentication required`. O gateway captura o mesmo `Authorization` da sessão autenticada e o mantém somente em memória.

Remoção (bloqueada): `DELETE /rest/v1/reviews?id=eq.{USER_ID}_{GAME_ID}`.

### Wishlist
Adicionar: `POST /rest/v1/user_watchlist_games?on_conflict=user_id%2Cgame_id` (bloqueado).
Remover: DELETE (bloqueado).
RPCs: `get_profile_watchlist`, `get_game_watchlist_count`.

### Perfil
Leitura: `POST /rest/v1/rpc/get_my_profile`. Atualização: `POST /rest/v1/rpc/update_my_profile` (bloqueada; sem helper).
`favorite_games` com id/slug/title/coverUrl, limite visual 5. `playing_now` com platform, limite 10. Ao atualizar, ler perfil atual e preservar campos não migrados.

### Outras descobertas
`get_public_user_by_username`; `get_user_external_accounts_public`; `get_user_activity_heatmap`; `check_veterano_badge`; `list_user_external_library`; `count_user_external_library`; `get_profile_watchlist_facets`; tabelas de listas, comentários, badges, seguidores e notificações.

Biblioteca externa é de serviços como Steam, não a biblioteca principal. Jogos do perfil vêm de reviews e Wishlist; favoritos e Playing Now são configurações do perfil.

### Gateway de execução (Etapa 6)
- Preparação/confirmação são locais; nenhuma chamada mutável foi feita.
- Payload de criação estrito: `game_id`, `rating`, `text`, `liked` booleano; sem campos extras/`null`.
- `POST /api/reviews/upsert` não é considerado idempotente universal; idempotência é local.
- Timeout, conexão interrompida após envio, HTTP 4xx/5xx, identidade divergente, captura incompleta ou review ausente na releitura não provam não processamento: `write_ambiguous`, sem retry automático.
- Apenas falha comprovadamente anterior ao envio (`WriteNotSent`) é `failed`.
- Verificação posterior exige reler a review e comparar identidade, dono, jogo, nota, texto e liked, não apenas HTTP 2xx.
- Transporte real: `BrowserWriteTransport` no gateway, endpoint fixo, POST, `context.request`, `max_redirects=0`, sem retry, timeout explícito.
- `MIGRATION_ALLOW_WRITES=true` e `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=true` são obrigatórios; ambos falsos por padrão.
- Execução real nunca foi realizada; testes usam sessão simulada e mocks.

## Política aplicada ao navegador
- Liberado: GET/HEAD leitura, `/api/search/games`, `/api/igdb/query`, RPCs de leitura (`get_*`, `public_get_*`, `list_*`, `count_*`, `check_*`) na API/Supabase, refresh Firebase.
- Bloqueado: `/api/reviews/upsert`, `user_watchlist_games`, `update_my_profile`, qualquer DELETE/PATCH/PUT, POST desconhecido.
- POST de sign-in do Firebase só no fluxo `browser login`.

## `platforms` e avaliações somente nota (2026-11-26)

O frontend atual envia `platforms` (array de nomes) além de `game_id`, `rating`,
`text` e `liked`. O gateway ainda envia quatro campos; o contrato real de
`platforms` (obrigatório/opcional, `[]`/`null`) não foi testado. Ver
`docs/platforms-contract.md`.

A origem não possui plataforma em `games.json` (1.114/1.114 nulos). Portanto não
há dado de plataforma para migrar sem coleta complementar.

Avaliações somente nota são representadas com `text=""` e `evaluation_kind="rating_only"`.
O destino já contém uma review com texto vazio (RE4 `game_id` 974), o que mostra
que o backend aceita esse estado; nenhuma escrita foi feita nesta etapa.

## Pendências
- Wishlist e perfil autenticados ainda não usam o mecanismo de snapshot.
- Contrato de `liked` para omissão/`null`/default do backend continua desconhecido; não testar por escrita.
- Aprovação por hash, preflight e gateway existem; transporte real está implementado, mas desabilitado por dois gates e nunca executado. Escrita real continua futura e bloqueada.
- Limites validados no servidor; remoção de favoritos/Playing Now; rate limits/paginação.
- DOM fallback de busca não testado no site real; API same-origin foi suficiente.
