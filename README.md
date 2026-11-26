# Stash → Já Zerei Migration

Ferramenta local para exportar jogos e reviews do Stash, relacioná-los ao catálogo do Já Zerei e preparar uma migração revisável e auditável.

> **Estado (2026-11-26, Etapa 7):** fluxo visual reduzido a três etapas (Preparar, Revisar e executar, Conferir resultado) com seção Avançado. O perfil Chromium e o estado transitório do wizard foram descartados; a próxima preparação exige login manual. Coletas, snapshots, decisões humanas, planos, aprovações, preflights e execuções anteriores foram preservados. A execução `20261125T221529Z-...` mantém `pseudoregalia` como `write_ambiguous` (HTTP 401) e bloqueia nova preparação até reconciliação autenticada. Nenhuma escrita real foi executada nesta etapa; os dois gates continuam desligados.

## Objetivos
- Exportar jogos e reviews de MikeMequis1 no Stash.
- Preservar dados de origem em JSON independente.
- Relacionar jogos ao catálogo Já Zerei/IGDB.
- Migrar reviews, Wishlist e configurações selecionadas.
- Exigir aprovação explícita antes de escrever.
- Produzir relatórios de conflitos, falhas e verificação.

## Fontes
- https://stash.games/users/MikeMequis1
- https://jazerei.com
- Pesquisa: POST https://jazerei.com/api/search/games

## Documentação
- [Stash](docs/stash.md) · [Já Zerei](docs/jazerei-api.md)
- [Arquitetura](docs/architecture.md) · [Plano](docs/implementation-plan.md) · [Segurança](docs/security.md)
- [Registro de execução](docs/execution-log.md)
- [Prontidão da Etapa 7](docs/readiness.md) · [Procedimento da primeira escrita](docs/operations-first-write.md)
- [Interface de revisão de correspondências](docs/review-ui.md)
- [Desenho do reset e UX em três etapas](docs/2026-11-26-reset-ux-design.md) · [Inventário de dados](docs/2026-11-26-data-inventory.md)

## Instalação

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -r requirements-browser.txt
.venv\Scripts\python -m playwright install chromium
Copy-Item .env.example .env   # preencher apenas localmente
```

O Playwright é opcional para a suíte offline, mas obrigatório para `--method browser`.
O navegador é Chromium próprio do Playwright; **nunca** usa o perfil pessoal do Chrome.

## Métodos de coleta

- `http`: cliente `requests` original. Funciona para o catálogo público do Já Zerei; no Stash recebe HTTP 403 do Cloudflare.
- `browser`: Chromium com perfil persistente do projeto. Necessário para o Stash (headful passa o desafio Cloudflare) e útil para o Já Zerei autenticado.

Ambos produzem os mesmos modelos Pydantic e os mesmos arquivos JSON.

## Comandos essenciais

```powershell
.venv\Scripts\python -m src.cli status
.venv\Scripts\python -m src.cli export --kind both --max-pages 1 --method browser
.venv\Scripts\python -m src.cli search "Sonic Unleashed" --limit 3 --method browser --browser-headless
.venv\Scripts\python -m src.cli browser status
.venv\Scripts\python -m src.cli browser status --verify
.venv\Scripts\python -m src.cli browser login jazerei
.venv\Scripts\python -m src.cli browser reviews
.venv\Scripts\python -m src.cli browser review-state --game-id 26570 --slug demolition-girl --headless
.venv\Scripts\python -m src.cli browser detail sonic-unleashed --headless
```

`browser review-state` exige identidade esperada: `JAZEREI_USER_ID` no ambiente local ou o UID descoberto por `browser login jazerei`. O identificador não é inventado nem hardcoded: é comparado com a sessão e com `get_my_profile`.

### Decisão humana de `liked` (Etapa 3)

```powershell
.venv\Scripts\python -m src.cli migration liked decide `
  --source-ref demolition-girl `
  --game-id 26570 `
  --value false `
  --review-file data/stash/runs/<run-id>/reviews.json

.venv\Scripts\python -m src.cli migration liked list
.venv\Scripts\python -m src.cli migration liked show `
  --source-ref demolition-girl `
  --game-id 26570 `
  --review-file data/stash/runs/<run-id>/reviews.json
```

Regras: sem default silencioso; `true`/`false` por decisão humana ou pela política automática explícita (`≥7` true, `≤5` false, `5 < nota < 7` unresolved); decisão vinculada ao hash da review; troca preserva a decisão anterior; decisão desatualizada fica `stale` e bloqueia o plano. Review já existente no destino não gera atualização.

### Coleta dirigida (Etapa 1)

Reproduz o piloto sem script externo:

```powershell
.venv\Scripts\python -m src.cli export `
  --kind both `
  --method browser `
  --slug demolition-girl `
  --game-page 9 `
  --review-page 4 `
  --run-id pilot-demolition-girl-20261124-v4
```

Cada execução grava em `data/stash/runs/<run-id>/`:

- `games.json` / `reviews.json` (somente quando há evidência de coleta válida);
- `export-report.json` com modo, páginas, falhas e resultado;
- `manifest.json` com SHA-256 e tamanho de cada arquivo;
- `html/` opcional quando `--save-html` é usado.

Exit codes: `0` completo/dirigido/vazio válido; `1` parcial; `2` falha ou alvo não encontrado. `--overwrite` exige `--run-id` explícito; sem ele, runs nunca são sobrescritos. Uma falha de HTTP 403 não cria `games.json`/`reviews.json` vazios e não substitui um run válido.

`--max-pages 1` sem páginas explícitas é uma coleta parcial por definição; use `--all-pages` para completude.

### Estado autenticado de um jogo (Etapa 2)

```powershell
$env:JAZEREI_USER_ID = '<uid da conta>'   # ou .env local
.venv\Scripts\python -m src.cli browser review-state `
  --game-id 26570 `
  --slug demolition-girl `
  --page-size 50 `
  --headless
```

O comando:

- reutiliza o perfil persistente do Playwright;
- confere o UID da sessão e o `id` retornado por `get_my_profile`;
- aborta se a identidade divergir;
- pagina `/rest/v1/reviews` com `Range`, deduplica por `id` e valida `user_id` de cada registro;
- grava `data/jazerei/runs/<run-id>/review-state/<slug>-<game-id>.json` + `manifest.json`;
- distingue `exists:true`, `exists:false` e captura inconclusiva;
- não sobrescreve snapshots anteriores.

Falhas geram `capture-failure.json`/manifesto de falha, sem snapshot válido. Page size padrão: 50 (`JAZEREI_PAGE_SIZE`).

`browser open|login` abre janela headful. `browser close` apenas marca o encerramento: cada comando já fecha o navegador e preserva cookies/localStorage no perfil.

### Problemas de login
- Se a janela for fechada durante o login, a sessão já gravada é preservada; rode `browser status --verify`.
- Google pode bloquear login em navegador automatizado. Use login por e-mail/senha quando disponível; o programa não contorna challenge do provedor.
- POSTs de autenticação Google só são liberados dentro de `browser login`; coleta normal continua estritamente read-only.
- Saída de `browser login` mostra requisições bloqueadas (sem headers/tokens) para diagnóstico.

Correspondência, plano e validação:

```powershell
.venv\Scripts\python -m src.cli match --games data/stash/runs/<run-id>/games.json --method browser --browser-headless

.venv\Scripts\python -m src.cli plan `
  --matches data/migration/demolition-girl/matches.json `
  --reviews data/stash/runs/<run-id>/reviews.json `
  --games data/stash/runs/<run-id>/games.json `
  --destination-snapshot data/jazerei/runs/<snapshot-run>/review-state/<slug>-<id>.json `
  --liked-decisions data/migration/liked-decisions.jsonl `
  --plan-id <plan-id>
```

O plano agora é gravado em `data/migration/plans/<plan-id>/`:

- `plan.json`;
- `manifest.json` com origem, destino, matching, decisões, hash canônico e hashes de arquivos.

O campo legado `approved: true` **não autoriza** nada. A autorização é separada.

### Aprovação e consulta

```powershell
python -m src.cli migration verify-plan --plan-id <plan-id>
python -m src.cli migration show --plan-id <plan-id>
python -m src.cli migration approve --plan-id <plan-id> --confirm <plan-id>
python -m src.cli migration approval-status --plan-id <plan-id>
python -m src.cli migration approvals list
```

- Aprovação exige proveniência completa, manifesto íntegro, validação estrutural sem erros e confirmação exata do `plan_id`.
- Aprovações são append-only em `data/migration/approvals.jsonl`.
- Alteração de origem, snapshot, decisões, correspondências, manifesto ou ação invalida a aprovação (`stale`).
- Nenhuma aprovação executa migração; preflight e gateway de escrita são etapas futuras.

### Preflight de releitura

```powershell
python -m src.cli migration preflight --plan-id <plan-id> --headless
python -m src.cli migration preflight-show --preflight-id <preflight-id>
python -m src.cli migration verify-preflight --preflight-id <preflight-id>
```

O preflight:

- exige plano íntegro, proveniência verificável e aprovação atual;
- revalida a identidade autenticada;
- relê todas as reviews uma vez e gera um snapshot novo por jogo, sem reutilizar snapshot antigo;
- compara semanticamente `before` aprovado × estado atual (`rating`, `text`, `liked`, IDs, existência);
- detecta review criada, removida ou modificada após a aprovação;
- classifica ações não suportadas (Wishlist/favoritos/Playing Now) como `unsupported` e bloqueia;
- grava `data/migration/preflights/<preflight-id>/preflight.json` + snapshots + manifest;
- não sobrescreve resultados/snapshots anteriores;
- não executa escrita.

Um preflight compatível não autoriza escrita nem substitui aprovação/preflight futuro.

### Gateway de execução simulada

```powershell
python -m src.cli migration execution-prepare --plan-id <plan-id> --preflight-id <preflight-id>
python -m src.cli migration execution-show --execution-id <execution-id>
python -m src.cli migration execution-status --execution-id <execution-id>
python -m src.cli migration execution-validate --execution-id <execution-id>
python -m src.cli migration execution-confirm --execution-id <execution-id> --action-id <action-id> --confirm <execution-id>
python -m src.cli migration execution-verify --execution-id <execution-id>
python -m src.cli migration execution-reconcile --execution-id <execution-id>
python -m src.cli migration execution-resolve --execution-id <execution-id> --action-id <action-id> --snapshot <snapshot.json>
python -m src.cli migration execution-run --execution-id <execution-id> --action-id <action-id> --confirm <execution-id>
```

### Revisão visual das correspondências

```powershell
python -m src.cli review-ui --port 8765 --open
```

A interface local permite revisar os 76 jogos, aprovar/rejeitar candidatos, pesquisar o catálogo, registrar ausência de correspondência e decidir `liked`. Também mostra a categoria Stash (`Mod`, `Remaster`, `Expanded game`, `Port`, `Bundle`), permite editar nota/texto/status/progresso preservando o original e registra aceitações editoriais dos alertas. Ela grava apenas decisões locais em `matches.json`/`liked-decisions.jsonl`, overrides append-only, ratificações coletivas, aceitações editoriais e histórico; não executa escrita no Já Zerei.

O login manual em `python -m src.cli browser login jazerei` descobre o UID da conta e grava apenas esse identificador em `data/browser/session-identity.json`. O preflight usa esse valor quando `JAZEREI_USER_ID` não está definido; `browser status --verify` também atualiza a identidade de uma sessão já logada. O arquivo não contém tokens ou cookies.

Para proveniência de destino do plano consolidado:

```powershell
python -m src.cli browser account-reviews --run-id <run-id>
```

- O gateway prepara execução somente após plano/manifesto, aprovação atual, preflight compatível e posterior à aprovação, identidade e payload validados.
- A confirmação é documental e vinculada a execution/plano/manifesto/preflight/ação/payload/identidade.
- `execution-run` é o único caminho real e exige, simultaneamente: `MIGRATION_ALLOW_WRITES=true`, `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=true`, execução preparada/validada, preflight compatível, confirmação individual e `--confirm` exatamente igual ao `execution_id`. Ambos os gates são falsos por padrão.
- Clientes do Já Zerei são somente leitura; não há helper público de escrita, `write_json` ou método HTTP genérico. O transporte real vive apenas no gateway.
- Criação de review é a única operação suportada; atualização/remoção/Wishlist/favoritos/Playing Now bloqueiam.
- Timeout, conexão interrompida após envio, HTTP 4xx/5xx, identity divergente, captura incompleta ou review ausente na releitura viram `write_ambiguous` e não são reenviados. Só falha comprovadamente anterior ao envio (`WriteNotSent`) vira `failed`. Escritas interrompidas são classificadas por `execution-reconcile`; `execution-resolve` aceita apenas snapshot autenticado e conclusivo, mantém ausência como ambígua e nunca escreve.
- A resposta HTTP nunca é usada como prova de sucesso nem como prova de não processamento: a verificação posterior relê o destino e compara nota/texto/`liked`/dono.

Aprovação, preflight e confirmação de execução são evidências distintas. Nenhuma delas, isoladamente, autoriza escrita real. Ver [prontidão](docs/readiness.md) e [procedimento da primeira escrita](docs/operations-first-write.md).

O plano nasce com `approved: false`. A decisão humana desta fase é editar `matches.json`/`plan.json`; não há comando de aprovação em lote. Sem decisão `liked` registrada, a ação de review fica `unresolved` e o validador bloqueia. Review já existente no destino é mantida sem atualização. O estado atual do Já Zerei deve vir dos snapshots de `review-state`; a aprovação por hash está implementada, mas não substitui o preflight futuro.

## Estrutura implementada

```
src/
  cli.py              status/export/search/match/plan/validate/verify/browser
  config.py           ambiente, flags (allow_writes + transporte real), perfil do navegador
  export_run.py       classificação de runs, exit codes e regra de escrita
  transport.py        HTTP somente-leitura: timeout, retry GET/HEAD, POST de consulta allowlisted
  storage.py          JSON/HTML, hashes SHA-256 e manifesto
  stash/              client, pagination, parser, games, reviews
  jazerei/            auth, client, catalog, reviews, watchlist, profile, state, snapshots (somente leitura)
  migration/          normalize, liked, matcher, planner, approval, preflight, preflights, execution, executions, validator, importer, verifier
  browser/            session, policy, stash, jazerei
  models/             source, normalized, destination, snapshot, approval, preflight, execution
tests/                parsers, plano, segurança, sessão Playwright, coleta e integridade
```

## Resultados reais (2026-11-23/25)

- HTTP direto ao Stash: **403 Cloudflare** em jogos e reviews.
- Chromium headful: perfil público lido; **30 jogos** e **6 reviews** na primeira página; **38 páginas de jogos** e **13 de reviews** detectadas.
- Headless no Stash: desafio Cloudflare permanece. Use headful.
- Catálogo Já Zerei: busca pública funcionou via HTTP e browser, com resultados idênticos.
- Sessão persistente: cookies e localStorage sobrevivem ao fechamento/reabertura.
- Login do Já Zerei: **autenticado** via Google; `status --verify` retornou `logged_in: true`.
- Leitura autenticada: captura de `/rest/v1/reviews` retornou 1 review da conta.
- Demolition Girl antigo: jogo na página 9, review na página 4.
- Demolition Girl novo run (`pilot-demolition-girl-20261124-v4`): 1 jogo + 1 review, resultado `directed`, exit `0`, manifesto verificado, dados **idênticos** aos artefatos antigos.
- Primeira tentativa do novo run (`...-20261124`): Cloudflare impediu as páginas; resultado `failed`, exit `2`, sem arquivos de dados. O run anterior não foi sobrescrito.
- Etapa 2: identidade confirmada por IndexedDB + `get_my_profile`; snapshot vazio válido para 26570 e snapshot existente para 974; ambos com manifesto íntegro e sem token.
- Etapa 4: plano real do Demolition Girl gerado com proveniência completa, mas **bloqueado** por falta de decisão `liked`; manifesto/hash gerados, nenhuma aprovação criada.
- Etapa 5: preflight do plano bloqueado recusado sem abrir o navegador; evidência bloqueada íntegra. Fixtures demonstram compatível, conflito e inconclusivo.
- Reviews/Wishlist/perfil autenticados: reviews por jogo implementadas; Wishlist/perfil ainda não consultados.
- Etapa 9: coleta completa `stage9-full-stash-browser-v2-20261125` com **1.114 jogos**, **76 reviews** e **76 detalhes**. Tentativa HTTP anterior falhou com 403; tentativa browser intermediária ficou parcial por Cloudflare; nenhum run anterior foi sobrescrito.
- Correspondências: **66 fortes aprovadas automaticamente**, **6 ambíguas** (1 aprovada manualmente, 5 pendentes), **4 sem candidato**. Nenhuma correspondência ambígua foi aprovada automaticamente.
- Destino: **1 review** (RE4, `game_id` 974), nenhuma sobreposta à origem. `liked` automático: 59 decisões (`≥7` true, `≤5` false) + 2 decisões humanas; 15 unresolved (nota intermediária ou sem destino).
- Transporte real implementado no gateway e desligado por dois gates; **nenhuma escrita real**.

## Princípios
1. Leitura primeiro; escrita desabilitada por padrão.
2. Nunca versionar tokens, cookies, chaves, perfis ou exports reais.
3. Não sobrescrever reviews sem aprovação.
4. Não truncar silenciosamente Playing Now.
5. Preservar dados originais e trilha de auditoria.
6. Nenhuma requisição de escrita no Já Zerei nesta fase.

## Status
- [x] Descoberta inicial do Stash
- [x] Operações de reviews, Wishlist, perfil e catálogo no Já Zerei
- [x] Exportador Stash HTTP (implementado; bloqueado pelo Cloudflare)
- [x] Exportador Stash browser (validado com dados reais)
- [x] Cliente Já Zerei read-only (catálogo público validado)
- [x] Correspondência e revisão de catálogo (candidatos + revisão manual)
- [x] Plano de validação (aprovação manual/programática)
- [x] Camada Playwright e política read-only
- [x] Login manual do Já Zerei validado
- [x] Ensaio do piloto Demolition Girl: correspondência, plano e validação
- [x] Etapa 1: coleta dirigida, runs versionados, manifesto e exit codes
- [x] Etapa 2: identidade, paginação completa e snapshots autenticados
- [x] Etapa 3: política explícita de `liked` e decisões humanas
- [x] Etapa 4: plano com manifesto e aprovação humana por hash
- [x] Etapa 5: preflight autenticado com releitura e comparação
- [x] Etapa 6: gateway simulado, confirmação e verificação posterior
- [ ] Etapa 7: Wishlist, favoritos e Playing Now
- [ ] Execução real (autorização futura explícita)

Endpoints documentados são observações empíricas do DevTools/navegador, não documentação oficial; podem mudar.
