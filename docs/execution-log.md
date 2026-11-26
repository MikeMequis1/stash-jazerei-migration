# Registro de execução

Registro cronológico complementar aos documentos principais. Não substitui arquitetura, plano, segurança ou descobertas.

## 2026-11-23 — Automação de navegador e primeiro ensaio integrado

### Fase 1 — Auditoria
- Repositório: estrutura `src/` + `docs/` + `tests/`; Git com alterações não commitadas da etapa anterior preservadas.
- Dependências novas: `playwright>=1.45,<2` (instalada 1.65.2), Chromium 159.0.8154.6.
- Baseline: 31 testes passando; Ruff limpo; CLI `status/export/search/match/plan/validate/verify`.
- Diagnóstico: módulos HTTP, modelos, matcher/plano já funcionais; lacuna era navegador, política de escrita e seletores reais.

### Fase 2 — Implementação do navegador
Criado:
- `src/browser/session.py`: Chromium persistente, metadados sem segredos, status, close.
- `src/browser/policy.py`: classificação de requests e bloqueio de escrita.
- `src/browser/stash.py`: adaptador que reutiliza parsers/coletores.
- `src/browser/jazerei.py`: busca same-origin, DOM fallback, detalhe, captura de reviews, login evidence.
- CLI `browser open/login/status/close/reviews/detail`; `--method http|browser` em export/search/match.
- `tests/test_browser_policy.py`, `tests/test_browser_session.py`, `tests/test_browser_jazerei.py`.
- `requirements-browser.txt`; perfil em `data/browser/` ignorado.

Testes offline: política (leitura/write/desconhecido/Cloudflare), sessão real local com cookie/localStorage persistente, extração Stash, busca API vs HTTP, DOM fallback, detalhe, login evidence.

### Fase 3 — Ensaio real
1. `browser status`: perfil inexistente no início.
2. Stash headless: título “Um momento…”, desafio Cloudflare; primeira falha causada por política bloqueando POSTs do desafio. Corrigido para permitir apenas `/cdn-cgi/challenge-platform/` (incluindo `challenges.cloudflare.com`).
3. Stash headful: desafio passou; perfil “Marselo II”, 30 cards na página 1, 38 páginas, 76 reviews, 13 páginas de reviews.
4. Headless continuou no desafio; decisão: documentar necessidade de headful.
5. Export browser página 1: 30 jogos, 6 reviews; depois 6 detalhes e 2 divergências listagem/detalhe. Capa e status estavam errados inicialmente (ícone de status e primeiro `use`), corrigidos.
6. Descoberto seletor real do detalhe: `.game__review-comment`.
7. Descoberto que `MIGRATION_DATA_DIR` movia o perfil e criava sessão nova; execução de validação passou a usar `MIGRATION_BROWSER_PROFILE_DIR=data/browser/profile`.
8. HTTP Stash: 403 Cloudflare em jogos/reviews; relatório registra falha, sem dados inventados.
9. Já Zerei catálogo: busca pública HTTP e browser idênticas (Sonic Unleashed ID 5169, variantes). Detalhe correto em `/jogos/sonic-unleashed`, JSON-LD VideoGame.
10. Sessão: fechar/reabrir preservou cookies e localStorage; `browser status --verify` retornou `logged_in: false` e `login_visible: true`.
11. Política refinada para permitir POSTs de leitura observados (`/api/igdb/query`, RPC `public_get_top_reviewers`); 115 requisições, 0 bloqueadas na home.
12. Login manual não executado: requer ação do usuário; nenhuma senha foi solicitada ou armazenada.

### Fase 4 — Ensaio de um jogo
- Bloqueada: o identificador recebido foi o placeholder `SLUG_DO_JOGO`; é necessário o slug real.
- Também bloqueada a parte autenticada (review atual) até login manual.
- Nenhum plano de migração real foi gerado; nenhuma escrita executada.

### Decisões
- Browser complementa HTTP, sem parsers duplicados.
- Escrita bloqueada por política de rede + flag/flags de importador; nenhuma exceção.
- Cloudflare challenge é a única categoria POST de borda permitida.
- Perfil persistente é tratado como credencial sensível.
- Não marcar fases como concluídas sem teste real; Fase 4 permanece bloqueada.

## 2026-11-23 — Incidente de login Google
- Usuário tentou login Google e a janela foi fechada durante a espera; o CLI terminou com `TargetClosedError`.
- Correções:
  - `wait_for_login`/`login_evidence` convertem fechamento de página em `BrowserUnavailable` com mensagem clara;
  - `browser login` imprime resumo de requisições bloqueadas e retorna código 2 nesse caso;
  - `BrowserSession.wait`, `content` e `title` detectam janela fechada;
  - POSTs de autenticação Google (`accounts.google.com`, `oauth2.googleapis.com`, `/oauth2`, `/gsi`, reCAPTCHA) liberados **somente** em `allow_login=True`;
  - detecção de sessão ampliada para Firebase/Supabase/IndexedDB;
  - corrigido falso positivo: `firebaseLocalStorageDb` vazio não conta como login; exige chave `firebase:authUser` real.
- Nenhum cookie/token foi impresso. Sessão persistida permanece válida se o perfil foi gravado.
- Limitação: Google pode recusar login em navegador automatizado; nesse caso usar login por e-mail/senha (se disponível) ou conta Google em ambiente não automatizado. Não contornar challenge de provedor com credenciais.

## 2026-11-23 — Login Google confirmado
- Usuário concluiu login Google; `browser login` retornou `logged_in: true`.
- `browser status --verify`: `firebase_auth_marker: false`, `supabase_auth_marker: false`, `indexed_db_marker: true`; `login_visible: false`; 125 requisições, 0 bloqueadas.
- Marcador válido: chave `firebase:authUser` dentro de `firebaseLocalStorageDb`; DB vazio não conta.
- `browser reviews --headless`: captura de `/rest/v1/reviews`; 1 review da conta.
- Bloqueios de telemetria no login (18): `/api/sinal`, `play.google.com/log`, `signaler-pa.googleapis.com`; não afetaram autenticação.
- Ajuste: `identitytoolkit accounts:lookup` liberado como leitura de conta; RPCs `*_exists` liberadas como leitura.
- Nenhuma escrita; `allow_writes: false`.

## 2026-11-23 — Piloto Demolition Girl (read-only)
**Entrada:** slug `demolition-girl`, review Stash página 4, candidato Já Zerei 26570 informado pelo usuário.

**Stash**
- Jogo localizado na página 9 da biblioteca (270 cards varridos até achar).
- Card: título Demolition Girl, ano 2004, data exibida `20 May 2004`, status `archived`, nota 2.0, capa IGDB, plataforma ausente.
- Review página 4: nota 2.0, data `2024-10-22T22:42:39Z`, status `archived`, likes 1, comentários 0, progresso ausente.
- Detalhe individual: texto integral idêntico à listagem (239 caracteres); `text_listing` e `text_detail` preservados em `raw`.

**Já Zerei**
- Busca pública retornou somente ID 26570, slug `demolition-girl`, 2004, Tamsoft, PlayStation 2.
- Detalhe `/jogos/demolition-girl` confirmou JSON-LD `datePublished: 2004-05-20`.
- Conta autenticada: 4 respostas de `/rest/v1/reviews` na página do jogo, todas vazias; nenhuma review para 26570. Botão “Avaliar” presente, “Editar” ausente.

**Correspondência**
- Slug idêntico, título normalizado idêntico, ano idêntico; confiança 0.90.
- ID 26570 marcado `approved` como **seleção humana** (usuário informou o candidato), não por decisão automática do matcher.
- Nenhuma alternativa relevante; único resultado.

**Plano/validação**
- Ação única `review:demolition-girl`, `game_id 26570`, `before: null`, `after.rating 2.0`, texto integral, `liked: null`.
- Plano e ação permanecem `approved: false`.
- Validador: `plan_not_approved`, `action_not_approved`, `liked_semantics_pending`.
- Preflight: `allowed: false`; nenhuma escrita.
- Artefatos: `data/migration/demolition-girl/{stash/,jazerei/,matches.json,plan.json,validation.json,preflight.json,summary.json}`.

**Conclusão:** identidade, nota, texto, data e ausência de conflito validados. Único bloqueio de conteúdo é `liked`; aprovação humana e escrita continuam fora de escopo.

## 2026-11-24 — Etapa 1: coleta reproduzível e íntegra

**Auditoria confirmada antes da implementação**
- Coleta do piloto era script ad hoc; CLI só paginava a partir da página 1.
- `write_json` sobrescrevia por padrão; `cmd_export` retornava 0 após HTTP 403 e podia gravar listas vazias.
- Ordem de exceções da CLI tornava `FileNotFoundError`/`ValueError` inalcançáveis.
- `capture_reviews` não filtrava identidade explicitamente; modelo de destino não tinha `liked`.
- Aprovação não tinha hash/proveniência; `preflight` não relia o destino.
- Nenhuma escrita no Já Zerei.

**Implementado**
- `export` com `--slug`, `--game-page`, `--review-page`, `--run-id` e `--overwrite`.
- Coleta dirigida reutilizando parsers/coletores; páginas explícitas e falhas de página/parse.
- Runs em `data/stash/runs/<run-id>/`, staging antes de publicar.
- `manifest.json` com schema, run ID, outcome, exit code e SHA-256 de cada arquivo.
- `write_json` não sobrescreve sem `overwrite=True`.
- Outcome `complete/directed/empty/partial/target_not_found/failed`; exit `0/1/2`.
- Falha HTTP/Cloudflare não cria arquivos de dados nem substitui run válido.
- `--overwrite` exige run ID explícito; falha preserva run anterior e grava evidência em `*-failed-*`.
- Ordem de exceções da CLI corrigida; `FileExistsError` tratado.

**Testes**
- 70 testes passando; Ruff limpo.
- Fixtures: página válida, slug encontrado/ausente, página fora do intervalo, HTTP 403, parse failure, vazio válido, parcial, manifesto/tamper, overwrite/falha.

**Execução real**
- Tentativa `pilot-demolition-girl-20261124`: Cloudflare bloqueou páginas; outcome `failed`, exit 2, sem `games.json`/`reviews.json`; run anterior intacto.
- `pilot-demolition-girl-20261124-v4`: página 9 de jogos + página 4 de reviews; 1 jogo, 1 review, 1 detalhe; outcome `directed`, exit 0.
- Manifesto verificado (`verify_manifest == []`); hashes registrados.
- `games.json` e `reviews.json` do v4 idênticos aos artefatos antigos `data/migration/demolition-girl/stash/`.

**Pendências**
- Etapa 2 (não implementada): identidade, paginação completa, estado vazio/proveniência, uso da sessão Playwright sem token em env.
- `liked`, aprovação auditável, preflight com releitura, gateway de escrita e verificação pós-escrita continuam futuros.
- Sem commits/push.

### Próximos passos
1. Implementar Etapa 2 conforme proposta no `docs/implementation-plan.md`.
2. Resolver contrato de `liked` com decisão explícita.
3. Implementar aprovação por hash e preflight rigoroso.
4. Só depois, gateway de escrita e verificação, com autorização.

## 2026-11-24 — Etapa 2: snapshot autenticado do Já Zerei

**Pontos de integração usados**
- `BrowserSession`/perfil persistente existentes.
- `get_my_profile` (leitura) para identidade do servidor.
- IndexedDB Firebase para UID local.
- GET `/rest/v1/reviews` com headers capturados da própria aplicação.
- `storage.write_json`/`write_manifest` da Etapa 1.

**Implementado**
- `src/jazerei/state.py`: identidade, paginação `Range`/`Content-Range`, deduplicação, validação de `user_id`, snapshot canônico.
- `src/jazerei/snapshots.py`: runs `data/jazerei/runs/<run-id>/`, staging, manifesto, falha separada.
- `src/models/snapshot.py`: schema versionado e hash determinístico.
- `DestinationReview` preserva `user_id`, `liked`, contagens, `game_*`, `short_id`, `moderation_status`, gêneros e campos extras.
- CLI `browser review-state`.

**Identidade**
- UID local: `fKJ...` (valor completo apenas no snapshot/fonte local; não em logs).
- UID API: `get_my_profile.id`.
- Esperado: `JAZEREI_USER_ID`.
- Sessão/API/esperado/query devem coincidir; caso contrário não há snapshot.

**Paginação**
- page size 50 (configurável), `Range`/`Range-Unit: items`.
- Terminação comprovada por `content_range_total`, `short_page` ou `empty_page`.
- Página repetida, HTTP inesperado, JSON inválido, total não atingido, `user_id` divergente ou `max_pages` esgotado invalidam a captura.
- Deduplicação por `id`; contagens recebidas/únicas/duplicadas registradas.

**Validação real**
- `review-state-demolition-girl-20261124-v4`: `exists:false`, 1 página, 1 review recebida (de outro jogo), 0 duplicatas, hash `f7b3...`, manifesto `[]`, fontes `firebase_indexeddb` + `get_my_profile`.
- `review-state-resident-evil-4-20261124-v4`: `exists:true`, review de 974 com rating 10, texto vazio, `liked:false`, contagens e campos extras; hash `e4c8...`, manifesto `[]`.
- Nenhuma requisição de escrita.

**Incidente de segurança**
- O primeiro snapshot de 974 preservou `user_avatar` com `?token=...`.
- Corrigido: sanitização de chaves/URLs sensíveis antes de persistir; snapshots antigos foram higienizados e manifestos re-hashados.
- Novos snapshots não contêm `token=`, `apikey`, Authorization ou cookies.

**Testes**
- 96 testes passando; Ruff limpo.
- Cobertura: identidade, sessão ausente/divergente, paginação, vazio válido, duplicatas, página repetida, HTTP, JSON malformado, total incompleto, snapshot existente/ausente, sanitização, manifesto, no-overwrite, CLI e falha.

**Limitações**
- `JAZEREI_USER_ID` precisa estar configurado.
- Snapshot ainda não é lido automaticamente pelo planner.
- Wishlist/perfil não usam este mecanismo.
- `liked` apenas preservado; política é Etapa 3.
- Sem aprovação por hash/preflight/gateway de escrita.
- Sem commits/push.

### Próximos passos
1. Revisar os snapshots de 26570 e 974.
2. Implementar Etapa 3 (política de `liked`).
3. Integrar snapshots ao planner e à aprovação por hash.
4. Preflight, gateway e verificação em etapas posteriores.

## 2026-11-24 — Etapa 3: contrato e política de `liked`

**Investigação**
- Confirmado no frontend público: `liked` é recomendação do autor; `likes_count`/comments e `review_likes` são engajamento.
- GET autenticado confirma `liked` persistido (`false` na review existente).
- Não há evidência de contrato para omissão/`null`/default do backend; nenhum POST de teste foi feito. `GET /rest/v1/` (OpenAPI) retornou 401 “Secret API key required”, então o schema do banco não é acessível com a chave pública.
- Stash não fornece campo de recomendação; nota, texto, status, likes e comentários não são equivalentes.

**Implementado**
- `LikedState` (`unresolved`/`decided`/`stale`) e `LikedDecision` versionada.
- Registro append-only `liked-decisions.jsonl`, com `source_ref`, `game_id`, hash canônico da review, valor, timestamp, `decision_id` e `supersedes`.
- CLI `migration liked decide/list/show`; `--replace` preserva a decisão anterior.
- Planner resolve decisão por hash e bloqueia `liked_unresolved`/`liked_decision_stale`.
- Review existente no destino é registrada em `skipped` com `before` (incluindo `liked`), sem ação de atualização.
- `likes_count`/`comments_count` não são usados como `liked`.

**Testes**
- 105 testes passando; Ruff limpo.
- Casos: true/false, ausência, hash alterado, jogo diferente, sobrescrita sem `--replace`, review existente, valores de engajamento, CLI e validação.

**Limitações**
- Contrato de omissão/`null`/default do backend continua desconhecido; não dependemos dele.
- Não há integração automática das decisões com aprovação por hash (Etapa 4).
- Nenhuma escrita no Já Zerei.
- Sem commits/push.

### Próximos passos
1. Revisar a política e as decisões de `liked`.
2. Implementar Etapa 4 (aprovação por hash).
3. Etapa 5 (preflight) e Etapa 6 (gateway) somente após autorização.
4. Integrar snapshots e decisões ao planner de forma auditável.

## 2026-11-24 — Etapa 4: plano, manifesto e aprovação

**Implementado**
- Planos versionados em `data/migration/plans/<plan-id>/` (`plan.json`, `manifest.json`).
- Hash canônico determinístico (`canonical_json`, SHA-256) com proveniência de origem, destino, matching, decisões e ações.
- `plan_sha256`, `manifest_sha256` e `plan_file_sha256` distintos.
- Ações com IDs/hashes estáveis; campos de aprovação/execução excluídos do hash.
- Aprovações append-only em `data/migration/approvals.jsonl`, vinculadas a plano/manifesto/ações.
- CLI `migration show|verify-plan|approve|approval-status|approvals list`.
- Confirmação explícita exige `--confirm <plan-id>`.
- Status `approved/unapproved/stale/invalid/blocked`; plano legado com `approved:true` não autoriza.
- Gate de aprovação exige proveniência completa e validação estrutural.

**Cenário Demolition Girl**
- Plano real `pilot-demolition-girl-20261124-plan` gerado a partir do run Stash v4 e snapshot Já Zerei v4.
- Proveniência completa, manifesto íntegro.
- Plano bloqueado por `liked_unresolved`; `approve` recusou; nenhuma aprovação criada.
- Fixtures de teste demonstraram aprovação válida, alteração da origem/plano e transição para `stale`.

**Testes**
- 113 testes passando; Ruff limpo.
- Casos: hashes determinísticos, mudança de nota/texto/liked/game_id, manifesto/tamper, aprovação, confirmação errada, plano bloqueado, fonte/plano alterados, plano legado, append-only.

**Limitações**
- Aprovação não substitui preflight da Etapa 5.
- Não há execução/gateway.
- Manifesto usa caminhos de inputs para localizar matches/decisões; o hash lógico exclui caminhos.
- Sem commits/push.

### Próximos passos
1. Revisar o plano real bloqueado e as fixtures de aprovação.
2. Implementar Etapa 5 (preflight/releitura) somente após autorização.
3. Gateway de escrita e verificação posterior.
4. Não considerar o projeto pronto para migração.

## 2026-11-24 — Etapa 5: preflight com releitura autenticada

**Implementado**
- `prepare_preflight`: plano/manifesto/proveniência, decisões, validação, aprovação atual, identidade esperada.
- Reutilização de `capture_reviews_window` (uma autenticação, uma paginação) e `build_review_snapshot` por jogo.
- Comparação semântica `before` aprovado × estado atual: existência, `rating`, `text`, `liked`, `game_id`, `game_slug`, `id`.
- Resultado por ação (`compatible/conflict/unsupported/inconclusive/invalid`) e global.
- Captura incompleta/identidade divergente nunca vira ausência.
- Ações Wishlist/favoritos/Playing Now bloqueiam como `unsupported`.
- Persistência em `data/migration/preflights/<preflight-id>/` com snapshots + manifesto; sem sobrescrita.
- CLI `migration preflight`, `preflight-show`, `verify-preflight`.

**Cenário Demolition Girl**
- Plano real continua bloqueado (`liked_unresolved`).
- Preflight recusado antes de abrir o navegador; evidência bloqueada gravada e íntegra.
- Nenhuma snapshot de destino foi reutilizada como leitura atual.

**Testes**
- 123 testes passando; Ruff limpo.
- Casos: compatível, criada/modificada/removida, campos alterados, inconclusivo, identidade divergente, sem aprovação, ação não suportada, persistência/tamper, hash determinístico, CLI bloqueada.

**Limitações**
- Preflight não executa escrita nem autoriza execução.
- Ainda não relê Wishlist/favoritos/Playing Now.
- Não há gateway de escrita.
- Sem commits/push.

### Próximos passos
1. Revisar os resultados de preflight e limitações.
2. Etapa 6 (gateway de escrita) somente com autorização explícita.
3. Verificação pós-escrita e expansão para Wishlist/perfil.

## 2026-11-24 — Etapa 6: gateway simulado de execução

**Implementado**
- Modelos de execução, estados e transições; confirmação append-only por ação.
- Preparação exige plano/manifesto/aprovação/preflight compatível/identidade/payload.
- Somente criação de review; update/delete/Wishlist/favoritos/Playing Now bloqueados.
- Payload estrito `{game_id,rating,text,liked}`; `liked` booleano explícito.
- Leitura imediatamente anterior à escrita e verificação posterior com mocks.
- Transporte só aceito com `simulated=True`; sem transporte real na CLI.
- Timeout pós-envio vira `write_ambiguous`/inconclusive, sem retry automático.
- Persistência em `data/migration/executions/<id>/` com eventos/manifesto.
- CLI `execution-prepare/show/validate/confirm/status/verify`; nenhum comando de execução real.
- `execution-reconcile` classifica interrupções persistidas em `writing`/`verification_pending` como `write_ambiguous`; `execution-resolve` aceita apenas snapshot autenticado/conclusivo e não reenvia.
- Verificação posterior compara identidade, dono, `game_id`, nota, texto e `liked`; HTTP 2xx não basta.

**Revisão complementar — resultados ambíguos após envio**
- Antes: HTTP de erro, review ausente na releitura e identidade divergente podiam virar `failed`/`conflict`.
- Agora: timeout, conexão interrompida, HTTP 4xx/5xx, identidade divergente, captura incompleta e review ausente após envio são `write_ambiguous`; execução fica `inconclusive`.
- Só falha comprovadamente anterior ao envio (`WriteNotSent`) vira `failed`.
- `execution-resolve` mantém review ausente como ambígua; duas releituras ausentes não liberam novo POST.
- Enquanto houver ação ambígua, nenhuma ação subsequente é aceita; `running` pode voltar a `awaiting_confirmation` somente após ação verificada em plano multi-ação.
- Nenhum caso ambíguo faz segundo POST: transporte falso registra no máximo um envio por ação.

**Cenário Demolition Girl**
- Plano real continua bloqueado por `liked_unresolved`; `execution-prepare` recusado; nenhuma execução criada.
- Fixtures demonstraram preparação/confirmação, execução simulada única, timeout ambíguo, conflito pré-escrita, verificação divergente e bloqueio de transporte real.

**Testes**
- 165 testes passando; Ruff limpo.
- Nenhuma chamada mutável real; `allow_writes` falso.

**Limitações**
- Execução real indisponível nesta etapa.
- Gateway cobre apenas reviews; Wishlist/perfil futuros.
- Manifesto de execução é atualizado em transições; eventos preservam o histórico lógico.
- Aprovação/preflight/confirmação não autorizam execução real isoladamente.

### Próximos passos
1. Revisar gateway, estados e testes simulados.
2. Autorização explícita futura para primeira execução real.
3. Expansão de recursos e verificação final.

## 2026-11-24 — Etapa 7: auditoria operacional e prontidão

**Auditoria**
- Repositório: branch `main`, commit `4746280`; trabalho local não rastreado preservado; sem commit/push.
- `.env` ausente; perfil de navegador ignorado; nenhuma chave sensível nos JSON/JSONL.
- Cadeia de hashes do piloto revalidada: origem v4, snapshot v4, matches, plano, manifesto e preflight.
- Contrato `POST /api/reviews/upsert` classificado em fato/hipótese/desconhecido; nenhuma chamada mutável.
- Gateway revisado; helpers legados de escrita (`upsert_review`, perfil, watchlist) identificados como risco fora do gateway.
- Procedimento operacional, matriz de recuperação e requisitos do transporte real documentados.

**Piloto Demolition Girl**
- Origem e snapshot íntegros; review ausente no destino confirmada.
- Plano íntegro, mas não aprovado e com `liked_unresolved`.
- Aprovação e decisão `liked` ausentes; preflight `blocked`; nenhuma execução.
- Nenhum artefato do piloto foi alterado.

**Testes**
- 169 testes passando; Ruff limpo.
- Novos testes de superfície: helpers legados bloqueados sem flag, POST fora do retry, CLI sem comando de escrita/importação.

**Resultado**
- Prontidão global: `blocked`; autorização: `pending_human`.
- Nenhuma escrita real; `MIGRATION_ALLOW_WRITES=false`.

**Documentos**
- `docs/readiness.md`
- `docs/operations-first-write.md`

### Próximos passos
1. Revisar a auditoria e decidir `liked` explicitamente.
2. Gerar novo plano/aprovação/preflight após a decisão.
3. Implementar transporte real em etapa separada, se autorizado.

## 2026-11-24 — Etapa 8: remoção da superfície legada de escrita

**Refatoração**
- Removidos `upsert_review`, `update_profile`, `add_to_watchlist`, `remove_from_watchlist`.
- Removidos `JazereiClient.write_json`, `require_writes`, `post_json` e `public_post_json`.
- `HttpClient` agora é somente leitura: GET/HEAD + `post_read_json` com allowlist de consulta; métodos mutáveis recusados.
- `importer.execute` e `ImportRefused` removidos; `preflight` apenas resume bloqueios e nunca autoriza.
- `allow_writes=True` não recria helpers nem transporte.
- Leitura de perfil, watchlist, reviews e catálogo preservada.

**Testes**
- 179 testes passando; Ruff limpo.
- Testes arquiteturais: helpers ausentes, cliente sem API de escrita, POST mutável recusado, endpoints mutáveis ausentes, CLI sem execução, leituras funcionando.
- Nenhuma chamada mutável real; `allow_writes` falso.

**Riscos residuais**
- Python não é sandbox: código externo pode importar `requests` ou acessar atributos privados. A defesa é ausência de API pública, allowlist de leitura, gateway simulado e testes.
- Transporte real continua inexistente e não autorizado.

### Próximos passos
1. Revisar a nova fronteira de escrita.
2. Decidir `liked` explicitamente e gerar novos artefatos.
3. Implementar transporte real somente em etapa autorizada.

## 2026-11-25 — Etapa 9: encerramento da infraestrutura e transporte real

**Coleta**
- HTTP direto: 403 na página 1; run `stage9-full-stash-20261125` registrado como `failed`.
- Browser: primeira tentativa `stage9-full-stash-browser-20261125` parcial por Cloudflare; após abrir a sessão, `stage9-full-stash-browser-v2-20261125` completou.
- Resultado: 1.114 jogos (38 páginas), 76 reviews (13 páginas), 76 detalhes, 76 com texto.
- 11 divergências listagem/detalhe preservadas; `progress` ausente em 61; plataforma/rating ausentes em 1.114/416 jogos.
- Nenhum run anterior sobrescrito.

**Correspondências e destino**
- 76 correspondências geradas: 66 `candidate`, 10 `manual_review`, 0 aprovadas.
- 4 sem candidato no catálogo.
- Destino: 1 review (RE4, `game_id` 974); nenhuma sobreposta à origem.
- Relatório de 76 decisões `liked` pendentes em `data/migration/stage9/liked-pending.md`; nenhuma decisão criada.
- Nenhum plano/aprovação/execução real gerado, pois faltam decisões humanas.

**Transporte**
- `BrowserWriteTransport` implementado no gateway: endpoint fixo, POST, `context.request`, sem redirect, sem retry, timeout e payload estrito.
- Dois gates: `MIGRATION_ALLOW_WRITES` + `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT`, ambos falsos por padrão.
- `execution-run` implementado; recusa sem os dois gates e confirmação exata.
- Testes de transporte com sessão falsa: endpoint, payload inválido, timeout, 4xx/5xx, redirect, falha de conexão, 2xx sem review, divergência e verificação inconclusiva.

**Testes**
- 205 testes passando; Ruff limpo; formatter limpo.
- Nenhuma escrita real; `allow_writes` e transporte real permanecem desligados.

**Riscos residuais**
- Contrato real do backend e idempotência continuam desconhecidos.
- `BrowserWriteTransport` nunca foi usado contra o serviço real.
- 66 candidatos e 10 ambíguos exigem revisão humana; 76 `liked` pendentes.

### Próximos passos
1. Revisar/aprovar correspondências humanamente.
2. Registrar decisões `liked`.
3. Gerar plano/aprovação/preflight e autorizar uma única execução.

## 2026-11-25 — Interface local de revisão de correspondências

**Implementado**
- Servidor stdlib + UI estática (`web/review.html/css/js`) sem dependência nova.
- Painel, filtros, lotes S9-001..076, tela individual, navegação e progresso.
- Aprovação/rejeição por candidato, seleção manual via catálogo somente-leitura, sem correspondência e pendente.
- Categoria Stash extraída de `.games-list__category`; 180 jogos categorizados no run de games; sidecar `source-categories.json`.
- Categorias especiais não são autoaprovadas; sem candidato geram sugestão de não migração. 13 reviews com categoria, 4 sugeridas sem correspondência.
- Edição de nota/texto/status/progresso com original preservado e `review-overrides.jsonl` append-only.
- `liked` via `append_decision`; política automática: `strong_candidate` aprovado, `liked` ≥7 true, ≤5 false, entre 5 e 7 unresolved.
- Persistência em `matches.json` + histórico append-only; detecção de stale por hash de origem/manifesto.
- Aplicação real: 64 aprovações automáticas adicionais (66 fortes no total), 59 `liked` automáticos + 2 humanos; 15 unresolved. 3 aprovações automáticas de categoria especial revertidas para revisão.
- Prévia de plano via `build_plan`/`build_bundle`; sem aprovação/preflight/execução.
- Testes: 221 passando; Ruff limpo.

**Segurança**
- Nenhuma escrita no Já Zerei; gates permanecem falsos.
- Nenhum `execution-run`; nenhuma aprovação/decisão real criada.

### Próximos passos
1. Revisar os 76 jogos na interface.
2. Decidir `liked` em lotes.
3. Gerar plano e seguir aprovação/preflight.

## 2026-11-25 — Proveniência do plano consolidado

**Implementado**
- `AccountReviewsSnapshot`: captura única e versionada de todas as reviews da conta.
- Comando `browser account-reviews`.
- `destination_provenance` e `_verify_destination` aceitam snapshot completo de conta.
- `cmd_plan`/UI usam o snapshot de conta quando disponível.
- Plano `stage9-review-20261125T194000Z`: 70 ações, 6 unmatched, proveniência completa.
- `verify-plan`: `invalid: []`, `stale: []`, `blocked: []`.
- `approval-status`: `unapproved` (nenhuma aprovação criada).
- Snapshot: `stage9-account-snapshot-20261125` (1 review, completo, identidade verificada).

**Testes**
- 227 passando; Ruff limpo.
- Nenhuma escrita real.

### Próximos passos
1. Revisar as sete categorias especiais.
2. Aprovar o plano explicitamente.
3. Gerar preflight compatível e seguir execução controlada.

## 2026-11-25 — Ratificação humana em lote

**Implementado**
- `ratifications.jsonl` append-only com 61 correspondências e 56 `liked` automáticos ratificados.
- Validação por dimensão, itens exatos, `items_sha256`, `ratification_id`, hashes de plano/manifests e data.
- Interface/verify-plan distinguem `human_individual`, `human_batch_review`, `automatic_unreviewed`.
- Plano novo `stage9-review-20261125T201911Z`: 70 ações, 6 unmatched, 0 skipped.
- `verify-plan`: invalid [], stale [], blocked [], 61+56 batch, 0 automatic_unreviewed.
- `approval-status`: `unapproved` (nenhuma aprovação criada).
- Testes: 231 passando; Ruff limpo.

**Segurança**
- Nenhuma escrita no Já Zerei; gates falsos; nenhum commit/push.

## 2026-11-25 — Painel gráfico de migração

**Implementado**
- `MigrationService` integra aprovação, preflight, preparação e execução existentes.
- UI com seis etapas, botões de aprovar/preflight/preparar/executar e indicador de gates.
- Execução real continua condicionada aos dois gates; nenhum segundo transporte.
- Testes: 231 passando; Ruff limpo.

## 2026-11-25 — Aceitação dos alertas editoriais

**Implementado**
- `editorial-acceptances.jsonl` append-only para S9-008 e S9-067.
- Eventos `editorial_acceptance`, decisão `keep_association`, sem alterar plano/decisões.
- Auditoria: 70 ações idênticas, 0 alertas editoriais pendentes, 0 bloqueios técnicos.
- `verify-plan`: invalid [], stale [], blocked [], 61+56 batch, 0 automatic_unreviewed.
- `approval-status`: `unapproved`.
- Testes: 230 passando; Ruff limpo.
- Nenhuma escrita real; gates falsos.
## 2026-11-25 - Identidade descoberta no login

**Implementado**
- Login manual descobre o UID da conta (Firebase IndexedDB/localStorage ou `get_my_profile`) e grava `data/browser/session-identity.json`.
- Arquivo contém apenas `user_id`, origem, data e nota; nunca tokens, cookies ou cabeçalhos.
- `browser status --verify` atualiza a identidade de uma sessão já logada.
- `load_config` usa o UID salvo quando `JAZEREI_USER_ID` está ausente; a variável de ambiente continua tendo precedência.
- `read_session_user_id` também interpreta valores JSON em string no IndexedDB.
- Testes: cobertura de roundtrip, precedência, fallback e descoberta.

**Segurança**
- Nenhuma escrita no Já Zerei; gates falsos; nenhum commit/push.
## 2026-11-25 - Correcao do 401 no upsert real

**Incidente**
- Primeira execucao real: primeira acao (`pseudoregalia`, game 242148) recebeu `401 Authentication required`.
- A acao ficou `write_ambiguous`; execucao `inconclusive`.
- Verificacao autenticada posterior confirmou `exists:false` para 242148; nenhuma review foi criada.

**Causa**
- `BrowserWriteTransport` usava `context.request` apenas com cookies e `Content-Type`.
- O frontend envia `Authorization: Bearer <Firebase ID token>` no `POST /api/reviews/upsert`.

**Correcao**
- `authenticate()` captura o `Authorization` da sessao e o repassa ao transporte somente em memoria.
- Transporte aceita apenas `authorization` na allowlist de cabecalhos.
- UI desabilita execucao quando a execucao mais recente nao esta `prepared`/`awaiting_confirmation`.
- Execucoes sao listadas da mais recente para a mais antiga.
- Teste: `244 passed`; Ruff limpo.
## 2026-11-26 - Endurecimento causal (Etapa 5)

**Implementado**
- `validate_snapshot_semantics` e `canonical_account_hash` reutilizam os algoritmos de captura; validam estrutura, completude, identidade, `game_id`/slug, consistência de `result` e hash semântico.
- `before_read` é revalidado como `ReviewStateSnapshot`; schema inválido, captura/paginação incompleta, identidade divergente, jogo divergente, hash divergente ou review inconsistente resultam em `unverifiable` e nunca em ausência.
- Relatórios com snapshot semanticamente inválido/incompleto não confirmam ações.
- Plano inválido, stale ou com proveniência incompleta bloqueia globalmente `confirmed`; presença e conteúdo continuam visíveis.
- Relatório separa `capture_status`, `report_status`, `migration_status` e `causal_confirmation_status`.
- `blocked_reasons`/`controls.reasons` separam resolução somente leitura dos gates de escrita.

**Testes**
- 317 passando.
- Cobertura nova: before_read válido/inválido, hash divergente, conteúdo adulterado sem manifesto, legado sem hash, plano stale/inválido, estados independentes, gates e bloqueios read-only.
- Compatibilidade: os 18 `before_read` da execução real validam; snapshot de conta real valida; execução e plano continuam íntegros.

## 2026-11-26 - Fase C e relatório de verificação (Etapa 3)

**Implementado**
- `verification_report` correlaciona plano aprovado, execução, snapshot posterior, snapshot do plano e snapshots do preflight.
- Classificação independente por ação: estado da execução, presença, conteúdo e atribuição causal.
- Confirmação exige captura completa, identidade correta, review única, rating/texto/liked/slug/usuário coerentes, mesmo ID observado pela execução e ausência anterior.
- `liked` ausente no snapshot vira `unavailable`/inconclusivo; nunca é inferido.
- Job `verify_destination` captura a conta, grava snapshot versionado, gera relatório e persiste em `data/migration/verifications/<id>/`.
- Relatórios append-only com `report_sha256` e manifesto; listagem e consulta por ID/plano/execução.
- Endpoints `GET /api/verifications`, `GET /api/verifications/{id}` e `GET /api/report`.
- Fase C com resumo, contagens independentes, filtros, comparação lado a lado, histórico e detalhes.
- `WizardState` guarda apenas `last_verification_id` e a seleção atual.

**Preservação**
- Nenhuma captura real, nenhum POST, nenhum gate alterado.
- Nenhum plano, aprovação, decisão, snapshot ou execução existente alterado.
- Execução `...224312` continua com 18 verificadas, 51 prontas e 1 falha transitória.
- Execução `...221529` continua com `pseudoregalia` ambígua e não retomável.

**Testes**
- 296 passando.
- Cobertura: confirmação, ausência, divergência de rating/texto/liked, liked indisponível, duplicidade, ambiguidade, não execução, snapshot incompleto, identidade incorreta, caminho inválido, cancelamento sem publicação, persistência append-only, histórico, recuperação, endpoints e ausência de transporte.

## 2026-11-26 - Fase B operacional (Etapa 2)

**Implementado**
- `execution_detail` com resumo, contagens, ações completas, título do jogo, rating, `liked`, payload, `write_result`, verificação, confirmações, eventos, hashes e `verify_execution`.
- `list_executions` global com retomabilidade, ambiguidade, conflitos, falhas transitórias, bloqueios e controles derivados.
- Endpoints `GET /api/executions`, `GET /api/executions/{id}` e `POST /api/executions/{id}/reconcile`.
- Job `resolve_ambiguity` com `capture_review_state`, snapshot versionado e `resolve_ambiguous_action`; sem novo POST.
- Fase B com filtros por estado, tabela de ações, detalhe individual, cartões de falha/ambiguidade/conflito/interrupção, controles condicionados pelo backend e histórico global.
- Seleção de execução antiga troca o plano associado, sem torná-la executável.
- `cancel` recusa execução com escrita em andamento antes da reconciliação.

**Preservação**
- Nenhum POST real; nenhum gate alterado; nenhuma execução iniciada ou retomada.
- Execução `...224312` continua `inconclusive`, 18 verificadas, 51 prontas, 1 falha transitória.
- Execução `...221529` continua com `pseudoregalia` ambígua (HTTP 401), não retomável.
- Nenhum plano, aprovação, decisão ou manifesto alterado.

**Testes**
- 274 passando.
- Cobertura: detalhe real, listagem global, reconciliação sem navegador, resolução com snapshot, review ausente mantendo ambiguidade, conflito, seleção antiga somente leitura, endpoints HTTP e ausência de transporte de escrita.

## 2026-11-26 - Wizard da Etapa 1

**Implementado**
- `src/migration/setup.py`: `WizardState` atômico, descoberta de runs, abertura do Stash, login/verificação do Já Zerei, coleta e snapshot com progresso/cancelamento.
- `JobRunner` e lock global de operação em `src/review_ui.py`; jobs de conexão, coleta, snapshot, correspondências e preflight.
- Endpoints `GET /api/wizard`, `POST /api/wizard/select`, `GET /api/policy/preview`, `POST /api/policy`, `POST /api/jobs`, `GET /api/jobs/current` e `POST /api/jobs/cancel`.
- Interface em três fases, rail de oito passos, seletores de plano/execução, gates e identidade sempre visíveis; fase C marcada como futura.
- `ReviewService` usa o snapshot de conta ativo de forma consistente em `state()`, stale e geração de plano.
- Política automática removida do startup; aplicação exige prévia e confirmação.
- Correspondências novas só são adicionadas para slugs ausentes; decisões existentes preservadas.

**Preservação**
- Nenhuma escrita no Já Zerei; nenhum login, coleta ou snapshot executado durante a implementação.
- Nenhum plano, aprovação, snapshot ou execução existente alterado.
- Execução atual recuperada como `inconclusive`, 18 verificadas, 51 prontas, 1 falha transitória e `resumable=true`.
- Execução anterior mantém `pseudoregalia` como `write_ambiguous` (HTTP 401), visível e não retomável.

**Testes**
- Suíte completa, Ruff e `node --check` aprovados.
- Novos testes: inicialização somente leitura, proveniência do snapshot, política sem escrita, job/cancelamento, lock, correspondências incrementais, seleção bloqueada durante execução retomável e ativação do plano gerado.

## 2026-11-25 - Progresso, cancelamento e retomada na UI

**Implementado**
- Execucao da UI roda em thread e expoe `/api/migration/progress`.
- Barra de progresso com acao atual, `game_id`, status, motivo e contagens.
- Botao **Cancelar execucao** cooperativo: conclui a acao atual e cancela as restantes.
- Painel de erro estruturado com `code`, `detail`, `hint`, acao e execucao.
- Fechamento do navegador vira `browser_closed`; execucao e reconciliada.
- **Retomar execucao** reabilita apenas falhas transitorias de pre-escrita
  (`pre_write_read_failed`, `pre_write_read_inconclusive`, `write_failed_before_send`).
- Acoes `verified` e `write_ambiguous` nunca sao reenviadas automaticamente.
- Testes: 250 passando; Ruff limpo; `node --check` ok.

**Estado real observado**
- Execucao `20261125T224312Z-...`: 18 reviews verificadas; parada em `resident-evil-5`
  por `pre_write_read_failed`; 51 restantes; sem ambiguidade. Retomavel.
- Execucao anterior `20261125T221529Z-...`: 401 na primeira acao, `write_ambiguous`;
  nao retomavel automaticamente.
