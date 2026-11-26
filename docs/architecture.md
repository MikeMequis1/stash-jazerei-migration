# Arquitetura

Stash é origem; Já Zerei é destino. O formato intermediário é independente. Exports originais são imutáveis. Correspondências são explícitas e auditáveis. Toda escrita exige aprovação e verificação posterior.

## Fluxo
```
Stash → coletor/parsers → export bruto
Já Zerei (catálogo + estado atual) → correspondência
→ plano de migração → aprovação humana
→ importador controlado → verificação pós-escrita
```

## Camadas atuais

```
CLI
 ├── HTTP: src.transport → src.stash.client / src.jazerei.client
 └── Browser: src.browser.session → Playwright Chromium (perfil persistente)
                ├── src.browser.policy        permite leitura, bloqueia escrita
                ├── src.browser.stash         adaptador → coletores/parsers Stash
                └── src.browser.jazerei       DOM + API same-origin → modelos destino
```

O navegador é complementar ao HTTP. A coleta Stash via browser não reimplementa parsers: `BrowserStashClient` só entrega HTML e os coletores `collect_games`/`collect_reviews` continuam responsáveis por paginação, deduplicação, detalhe, relatório e modelos.

## Módulos implementados

```
src/
  cli.py            orquestração dos comandos
  config.py         Config via .env; allow_writes=False; perfil do navegador
  export_run.py     classificação de coleta e exit codes
  transport.py      HttpClient somente-leitura: timeout, retry GET/HEAD, POST de consulta allowlisted
  storage.py        JSON/HTML, SHA-256 e manifesto
  stash/{client,pagination,games,reviews,parser}.py
  jazerei/{auth,client,catalog,reviews,watchlist,profile,state,snapshots}.py
  migration/{normalize,liked,matcher,planner,approval,preflight,preflights,execution,executions,validator,importer,verifier,review_ui,ratification,editorial,setup,verifications}.py
  models/{source,normalized,destination,snapshot,approval,preflight,execution,verification}.py
  browser/{session,policy,stash,jazerei}.py
```

### Interface de revisão

`src/review_ui.py` é um servidor stdlib local + `web/review.{html,css,js}`. Lê a coleta e os snapshots, atualiza `matches.json` e `liked-decisions.jsonl`, registra histórico append-only e gera prévia de plano via `cmd_plan`. A escrita no Já Zerei só existe pelo gateway de execução e exige os dois gates; o wizard da Etapa 1 adiciona conexão, coleta, snapshot, jobs canceláveis, seleção persistente de plano/execução e leitura somente leitura do histórico. A Etapa 2 adiciona detalhe por ação, listagem global, reconciliação e resolução de ambiguidade com snapshot autenticado, sem novo POST. A Etapa 3 adiciona `verify_destination`, relatórios versionados em `data/migration/verifications/` e correlação explícita entre execução, presença no destino e conteúdo. A Etapa 5 endurece a Fase C: `before_read` é revalidado como snapshot completo, identidade/game_id/hash coerentes; snapshots têm hash semântico recalculado (`canonical_hash`/`canonical_account_hash`); plano inválido/stale bloqueia `confirmed`; e o relatório separa captura, processamento, migração e confirmação causal.

### Fronteira de escrita (Etapas 8–9)

- `src/jazerei/*` é somente leitura: sem helper mutável, sem `write_json`, sem método HTTP genérico.
- `HttpClient` aceita GET/HEAD e somente POSTs de consulta allowlisted (`/api/search/games`, `/api/igdb/query`, RPCs `get_/list_/count_/check_`).
- `importer` apenas resume bloqueios; não executa.
- A única fronteira de escrita é `src/migration/execution.py`, por meio de `BrowserWriteTransport`.
- `BrowserWriteTransport` usa a sessão persistente (`context.request`), endpoint fixo `https://jazerei.com/api/reviews/upsert`, método POST, sem redirects, sem retry, timeout explícito e payload estrito. O cabeçalho `Authorization: Bearer <ID token>` é capturado da própria sessão autenticada e mantido apenas em memória; sem ele o endpoint responde `401 Authentication required`.
- A execução pela UI roda em thread e publica progresso em `/api/migration/progress`; cancelamento é cooperativo entre ações. Retomada reabilita somente falhas transitórias de pré-escrita e nunca reenvia ações `verified` ou `write_ambiguous`.
- `execute_action` aceita transporte simulado ou, no caminho real, apenas `BrowserWriteTransport` com `allow_writes` e `enable_real_write_transport` verdadeiros.
- `MIGRATION_ALLOW_WRITES` é necessária, não suficiente; o segundo gate é `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT`, falso por padrão.
- O comando `execution-run` exige `--confirm` exatamente igual ao `execution_id`, além de execução preparada, aprovação, preflight, confirmação individual e leitura prévia.

## Gerenciador de navegador

`src.browser.session.BrowserSession`:

- inicia Chromium via `launch_persistent_context`;
- usa `data/browser/profile` (ou `MIGRATION_BROWSER_PROFILE_DIR`), exclusivo do projeto;
- preserva cookies e localStorage entre execuções;
- instala a política de rede antes da navegação;
- registra apenas método, host/caminho e decisão — nunca cabeçalhos, tokens ou corpos privados;
- fecha o contexto e o Playwright em `__exit__`;
- grava metadados não sensíveis em `data/browser/profile-meta.json`.

Cada comando abre e fecha o navegador. Não há processo em segundo plano; `browser close` apenas registra o encerramento.

## Sessão e autenticação

- Login é **manual** em `browser login jazerei`; o programa não solicita, preenche nem armazena senha.
- `allow_login=True` é usado apenas nesse comando; a política libera somente POSTs de sign-in do Firebase/Google.
- Coleta normal não permite POSTs de autenticação de conta, exceto refresh de token (`securetoken`).
- `browser status` informa apenas existência de perfil, cookies, localStorage e timestamps; `--verify` consulta o DOM para indício de login.
- Sessão expirada é detectada por ausência de marcador Firebase/DOM de logout; nesse caso o usuário refaz o login manual.

## Política de rede

`src.browser.policy` classifica cada request:

- GET/HEAD/OPTIONS de páginas, assets e API REST são leitura.
- POST permitido apenas para busca pública (`/api/search/games`), metadados (`/api/igdb/query`) e RPCs de leitura conhecidas ou prefixadas por `get_`/`public_get_`/`list_`/`count_`/`check_`.
- DELETE/PATCH/PUT: sempre bloqueados.
- POSTs de upsert, insert, update, delete, watchlist, favoritos e Playing Now: bloqueados.
- POST desconhecido: bloqueado e registrado.
- GET com marca de ação (`logout`, `delete`, `unwatch`, etc.): bloqueado.
- Desafio Cloudflare (`/cdn-cgi/challenge-platform/`) é permitido apenas para concluir a verificação anti-bot; não altera conta.

Requisição bloqueada é abortada com `blockedbyclient`. Não há fallback silencioso para escrita.

## Coleta e armazenamento versionado

`export` aceita coleta paginada normal ou dirigida:

- `--max-pages`/`--all-pages`: paginação dinâmica a partir da página 1;
- `--game-page`/`--review-page`: páginas explícitas, sem varredura;
- `--slug`: filtro exato; o alvo pode estar em qualquer página consultada.

Cada execução grava em `data/stash/runs/<run-id>/` (nunca no caminho legado por padrão):

```
games.json          somente com evidência de consulta válida
reviews.json        idem
export-report.json  modo, páginas, contagens, falhas, outcome e exit_code
manifest.json       SHA-256 e tamanho de cada arquivo
html/               HTML bruto opcional, separado dos dados normalizados
```

A gravação usa um diretório temporário e só é promovida ao run final após a
coleta. Falha de HTTP/Cloudflare não cria listas vazias. `--overwrite` exige
`--run-id` explícito e, se a nova coleta falhar, o run anterior é preservado
(evidência vai para um diretório `*-failed-*`).

`src.export_run` classifica o resultado em `complete`, `directed`, `empty`,
`partial`, `target_not_found` ou `failed`; exit codes `0`, `1` e `2` refletem
sucesso, parcial e falha. Uma lista vazia só é aceita como evidência quando
todas as páginas do escopo foram consultadas com sucesso.

## Avaliações: review escrita e somente nota (2026-11-26)

O universo de migração é formado por avaliações, não por status ou biblioteca:

- `games.json` + `reviews.json` → `src.migration.evaluations.build_evaluation_set`;
- `written_review`: nota válida e texto não vazio;
- `rating_only`: nota válida, texto `""`, `text_source="none"`, origem `games.json`
  e `source_url` da página do jogo;
- `no_rating`: descartado, independentemente de status.

Contagens confirmadas nos dados atuais: 1.114 jogos, 76 escritas, 622 somente nota,
416 sem nota, 0 inconsistências bloqueantes. O status do Stash é preservado apenas
como metadado de auditoria.

Cada `ReviewRecord` carrega `evaluation_kind`. O hash de decisão de `liked` exclui
esse campo para manter válidos os hashes históricos das reviews escritas.

O matching das avaliações é somente leitura e separado do `matches.json` histórico:

- `src.migration.evaluation_matching` preserva candidatos, scores, evidências e
  estados `candidate`, `manual_review`, `no_candidate`, `inconclusive`, `error`;
- falha de consulta nunca vira ausência de candidato;
- nenhum resultado é aprovado automaticamente;
- o run fica em `data/migration/evaluations/<run-id>/` com `evaluations.json`,
  `matches.json`, `match-report.json`, cache e manifesto;
- 622 rating-only e 7 written sem correspondência são os alvos do matching;
- 69 written preservam suas decisões históricas.

As 35 avaliações de origem já presentes no destino são protegidas: 17 verificadas
na execução cancelada e 18 reconciliadas por conteúdo, sem execução histórica
disponível. A avaliação de `spirit-of-the-north-enhanced-edition` permanece
sinalizada como conflito de identidade (jogo base, confiança 0,14). A review RE4
`game_id` 974 não tem origem no Stash e fica fora do escopo.

A política automática de correspondência/liked fica desabilitada quando há
avaliações somente nota; `liked` permanece `unresolved` até decisão humana.

## Revisão humana das avaliações (2026-11-26)

A etapa de revisão tem duas filas independentes na interface:

- **Correspondências**: 629 alvos do matching do run de avaliações; candidatos,
  revisão manual, sem candidato, inconclusivo, edições especiais e já no destino.
- **Liked**: as 622 avaliações `rating_only`, mais as decisões automáticas
  históricas que exigem reconfirmação.

Decisões de correspondência do run são append-only em
`data/migration/evaluations/<run-id>/match-decisions.jsonl`, com
`evaluation_run_id`, `evaluations_sha256`, `source_hash`, decisão anterior,
candidato, confiança, `edition_confirmed`, operador, UID da conta, motivo e hashes
do estado materializado. O histórico `stage9/match-decisions.jsonl` não é alterado.
`matches.json` continua materializado; divergência entre histórico e materializado
vira conflito visível e bloqueia elegibilidade, com reconstrução controlada e
backup.

`liked` continua em `data/migration/liked-decisions.jsonl`, append-only, agora com
`event_type` (`decision`, `reconfirmation`, `deferral`, `binding`),
`binding_status`, `game_id` opcional, versão do run, operador, valor anterior e
`supersedes`. Decisões source-only (`game_id=null`) são válidas enquanto o
`source_hash` não mudar. Decisão automática histórica fica
`requires_reconfirmation` até novo evento humano. "Ainda não decidi" em item já
decidido grava deferimento e volta a `unresolved`.

### Critério objetivo de lote

Uma correspondência só entra no lote quando o usuário a seleciona explicitamente
e, no backend:

- é `candidate` (não `manual_review`/aprovada);
- confiança >= 0,90;
- evidências incluem slug idêntico, título normalizado idêntico e ano idêntico;
- não é edição especial;
- não tem título duplicado na origem;
- não está no destino;
- não tem conflito histórico;
- versão da avaliação e `source_hash` atuais.

O lote é limitado a 50 itens, cada um gera evento próprio e falhas parciais são
reportadas por item. Edições especiais exigem confirmação individual com
justificativa, vinculada ao candidato. Não existe aprovação automática.

### Elegibilidade

`src.migration.eligibility` centraliza os bloqueios: correspondência aprovada,
edição confirmada, `liked` humano válido (ou source-only), presença no destino,
conflito histórico, estado inconclusivo/sem candidato, versão e hash atuais. A
presença no destino é independente da correspondência. Nesta etapa o motor é
exposto na UI, mas nenhum plano/preflight é gerado.

### Endpoints de revisão

- `GET /api/review/summary` — contadores, run e operador.
- `GET /api/review/item/{slug}` — detalhe do item.
- `GET /api/review/history/{slug}` — eventos de correspondência e liked.
- `GET /api/review/reconciliation` — conflitos entre histórico e materialização.
- `POST /api/match` — decisão individual, incluindo `confirm_edition`.
- `POST /api/match/batch` — lote explícito, máximo 50, revalidado por item.
- `POST /api/liked` — decisão, reconfirmação ou deferimento.
- `POST /api/liked/batch` — lote explícito, máximo 50, independente por item.
- `POST /api/review/reconcile` — reconstrução controlada com backup.

### Performance

O estado completo tem cerca de 2 MB para 698 avaliações. A UI usa paginação
client-side de 25/50 itens, filtros persistentes e carregamento de detalhes de
catálogo sob demanda; não recarrega o estado a cada página.

## Captura autenticada do Já Zerei (Etapa 2)

```
BrowserSession (perfil persistente)
  → /perfil
  → IndexedDB: uid da sessão
  → get_my_profile: id da conta (somente id é usado)
  → validação esperado == sessão == API
  → GET /rest/v1/reviews com Range (headers da própria aplicação)
  → deduplicação + validação de user_id
  → ReviewStateSnapshot + hash canônico
  → data/jazerei/runs/<run-id>/ + manifesto SHA-256
```

A identidade esperada vem de `JAZEREI_USER_ID` ou, quando a variável não está
definida, do UID descoberto no login e guardado em
`data/browser/session-identity.json`. O comando aborta se não houver sessão, se
o UID estiver ausente ou se sessão/API/esperado divergirem.

A paginação usa `Range`/`Range-Unit` e `Content-Range`, com page size
configurável. Ela só é considerada completa com critério comprovado
(`content_range_total`, `empty_page` ou `short_page`). Página repetida, HTTP
inesperado, JSON malformado, `user_id` divergente ou total não atingido
invalidam a captura.

Snapshots ficam em `data/jazerei/runs/<run-id>/review-state/` e têm manifesto
com SHA-256. Snapshots anteriores não são sobrescritos. Falhas geram
`capture-failure.json` e manifesto de falha, nunca um snapshot com
`exists:false`.

A sessão é usada apenas para GET e para as respostas de leitura que a própria
aplicação já executa. Nenhum token/cookie é persistido no snapshot; URLs com
`token`/`apikey` são sanitizadas antes da gravação.

## Etapa 2 — limites e próximos passos

- Wishlist e perfil autenticados ainda não usam este mecanismo.
- O planner ainda não consome automaticamente o snapshot.
- Aprovação por hash e preflight com releitura continuam planejados.
- A política de `liked` continua fora desta etapa; o valor apenas é preservado.


## Fluxo Browser → modelos

```
Playwright page.content() ─┐
DOM evaluate / fetch ──────┼→ parsers existentes → Pydantic → JSON
API capturada ─────────────┘
```

Stash:
`page HTML → src.stash.parser → GameRecord/ReviewRecord → collect_* → games.json/reviews.json/export-report.json`.

Já Zerei:
`fetch same-origin + DOM → parse_search_payload/parse_reviews_payload/parse_profile_payload → DestinationGame/DestinationReview/...`.

## `liked` e decisões humanas

`liked` é recomendação do autor, não `likes_count`. O Stash não possui campo
equivalente confirmado. O planner cria `LikedState` com status `unresolved`,
`decided` ou `stale`; sem decisão humana, a ação fica bloqueada.

Decisões são append-only em `data/migration/liked-decisions.jsonl`, vinculadas a
`source_ref`, `game_id` e hash canônico da review de origem. `--replace` cria
nova revisão e mantém a anterior via `supersedes`. Mudança de origem deixa a
decisão `stale`; ela não é reutilizada.

Review existente no Já Zerei é preservada (incluindo `liked`) e não gera ação de
atualização. A ausência de informação nunca é convertida em `false`.

## Plano, manifesto e aprovação

- `plan` grava `data/migration/plans/<plan-id>/plan.json` e `manifest.json`.
- O manifesto inclui origem (`run_id`, hash do manifesto e arquivos), destino (`snapshot_run_id`, hash de manifesto/snapshot, `response_hash`, `user_id`, jogo, existência), correspondências, decisões `liked`, ações e hashes.
- O hash canônico usa JSON com chaves ordenadas, Unicode preservado, sem espaços e sem campos voláteis (`created_at`, `inputs`, `approved` legado).
- `plan_sha256` cobre proveniência + ações; `manifest_sha256` cobre o manifesto; `plan_file_sha256` identifica o arquivo.
- Aprovações append-only ficam em `data/migration/approvals.jsonl`, vinculadas ao `plan_id`, `manifest_sha256`, `plan_sha256` e IDs das ações.
- Aprovação exige confirmação explícita do `plan_id`, proveniência completa e validação estrutural.
- Alteração de origem, destino, decisões, correspondências, plano ou manifesto deixa a aprovação `stale`/`invalid`.
- Aprovação não executa escrita; preflight e gateway são etapas seguintes.

### Hash canônico

- Serialização: `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)`.
- Unicode é preservado; chaves ordenadas; números em JSON; `null` explícito; arrays mantêm ordem lógica.
- Excluídos: `created_at`, caminhos de `inputs`, `approved`/`approved_by` legados, `executed`, `verified` e mensagens de erro.
- Incluídos: origem, hashes de manifestos/arquivos, destino, `response_hash`, matching, decisões, ações (tipo, jogo, antes/depois, `liked`), IDs/hashes.
- `plan_sha256` cobre proveniência + ações; `manifest_sha256` cobre o manifesto; `plan_file_sha256` é o hash do arquivo físico. O hash do arquivo não é o hash lógico.
- Aprovação referencia `manifest_sha256`/`plan_sha256`/IDs de ações, não apenas um booleano.

## Preflight

O preflight é posterior à aprovação e independente dela. Ele relê a sessão
autenticada, captura novamente todas as reviews uma vez, gera um snapshot novo
por jogo e compara semanticamente o `before` aprovado com o estado atual.

- Pré-condições: plano/manifesto/proveniência válidos, decisões, validação estrutural, aprovação atual e identidade esperada.
- Captura: `capture_reviews_window` reutiliza `authenticate` + `paginate_reviews`; snapshots novos por `build_review_snapshot`.
- Comparação: existência, `rating`, `text`, `liked`, `game_id`, `game_slug`, `id`.
- Ações não suportadas bloqueiam; captura incompleta é `inconclusive`; identidade divergente bloqueia.
- Persistência: `data/migration/preflights/<preflight-id>/preflight.json`, `snapshots/` e `manifest.json`; nunca sobrescreve.
- Preflight compatível não executa escrita e não altera aprovação.

## Gateway de execução (Etapa 6)

O gateway separa autorização documental de execução:

```
plan + approval + preflight compatible
  → execution-prepare (valida vínculos, payload, ações)
  → execution-confirm por ação (append-only, documental)
  → execute_action (somente transporte `simulated=True`)
       → leitura imediatamente anterior
       → POST único, sem retry
       → write_confirmed / write_ambiguous
       → releitura + verificação
  → execution-verify
```

- Somente criação de review (`before:null`, `kind:review`); update/delete/Wishlist/favoritos/Playing Now bloqueados.
- Payload exato `{game_id,rating,text,liked}`; `liked` booleano explícito, sem `null`, sem campos extras.
- Confirmação vinculada a execution/plano/manifesto/preflight/ação/payload/identidade.
- Transporte real não é fornecido ao gateway; `execute_action` recusa transporte não simulado mesmo com `allow_writes=true`.
- Preflight precisa ser compatível, íntegro, vinculado à mesma aprovação e não anterior a ela.
- Timeout, conexão interrompida após envio, HTTP 4xx/5xx, identidade divergente, captura incompleta ou review ausente na releitura → `write_ambiguous`, sem retry. Apenas falha comprovadamente anterior ao envio (`WriteNotSent`) vira `failed`.
- Verificação posterior exige identidade, `game_id`, dono, nota, texto e `liked` iguais ao payload; HTTP 2xx isolado não basta.
- Interrupção persistida em `writing`/`verification_pending` é classificada como ambígua por `execution-reconcile`; `execution-resolve` só aceita evidência autenticada e conclusiva, mantém review ausente como ambígua e nunca reenvia.
- Enquanto existir ação `write_ambiguous`, a execução fica `inconclusive` e qualquer nova ação é recusada.
- `execution.json`, `events.jsonl`, `confirmations.jsonl` e `manifest.json` em `data/migration/executions/<id>/`.
- Nenhuma execução real está disponível na CLI.
- Auditoria da Etapa 7: helpers legados de escrita foram identificados e removidos na Etapa 8. A Etapa 9 implementou o transporte real exclusivamente no gateway, com dois gates falsos por padrão; ver `docs/readiness.md` e `docs/operations-first-write.md`.

### Transições de execução

```text
prepared → blocked | awaiting_confirmation | cancelled
awaiting_confirmation → running | blocked | inconclusive | cancelled
running → completed | completed_with_errors | inconclusive | failed | awaiting_confirmation
inconclusive → completed | completed_with_errors   (somente resolução humana com evidência)
```

`running → awaiting_confirmation` existe para planos com múltiplas ações: após uma ação verificada, a próxima pode ser confirmada/executada. Se qualquer ação ficar ambígua, a execução vai para `inconclusive` e não continua.

### Transições de ação

```text
prepared → awaiting_confirmation | blocked | cancelled
awaiting_confirmation → ready | blocked | cancelled
ready → writing | conflict | failed | cancelled
writing → write_confirmed | write_ambiguous | failed
write_confirmed → verification_pending | write_ambiguous
verification_pending → verified | conflict | write_ambiguous
write_ambiguous → verified | conflict   (resolução humana com evidência; ausência permanece ambígua)
```

`verified`, `conflict`, `failed`, `blocked` e `cancelled` são terminais.

## Correspondência e conflitos

Usar pesquisa do catálogo; avaliar slug, nome normalizado, ano, desenvolvedora, plataformas e contexto de versão/remake/port. Nunca escolher apenas o primeiro resultado. Manter jogos sem correspondência; não substituir review existente sem aprovação; distinguir nota com texto vazio de ausência de review; não mapear status sem tabela validada.

## Limites de responsabilidade

- Coleta apenas lê e grava arquivos locais.
- Coleta dirigida não aprova correspondência nem escreve no destino.
- Navegador não clica em salvar/adicionar/remover; só navegação e leitura.
- Correspondência nunca aprova sozinha.
- Plano não executa escrita.
- Importador real pertence à Fase 5 e continua fora desta entrega.
- Aprovação auditável por hash e gateway de escrita ainda são Etapas 4–6 planejadas.
