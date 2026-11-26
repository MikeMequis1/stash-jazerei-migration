# Segurança e privacidade

> **Documento histórico do ciclo encerrado (2026-11-26).** As regras de segurança continuam válidas, mas alguns estados descritos abaixo foram superados: houve 17 escritas reais verificadas em 2026-11-26 e o transporte real existe. Os gates permanecem desligados. Para o estado consolidado, leia [`docs/project-handoff.md`](project-handoff.md).

Nunca versionar access_token, id_token, refresh_token, cookies, Authorization, API keys, .env real, exports reais, perfis de navegador ou dumps do DevTools com dados de sessão.

## Credenciais
Usar `.env` local ou armazenamento seguro do sistema. Placeholders provisórios:
```dotenv
JAZEREI_FIREBASE_API_KEY=
JAZEREI_USER_ID=
JAZEREI_ID_TOKEN=
JAZEREI_REFRESH_TOKEN=
```
Esses nomes não são contrato final de autenticação. O login do navegador não usa essas variáveis por padrão: a sessão manual vive no perfil persistente.

## Perfil persistente do navegador
- Diretório padrão: `data/browser/profile`, ignorado pelo Git junto com `data/browser/`.
- Nunca usa o perfil pessoal do Chrome.
- Contém cookies, IndexedDB e localStorage equivalentes a uma sessão autenticada.
- `profile-meta.json` guarda apenas timestamps, host e `headless`; nunca valores de cookie/token.
- O perfil não deve ser copiado, compartilhado, anexado a issues ou incluído em relatórios.
- `browser close` não apaga a sessão; para revogar, o usuário deve sair no site ou remover o diretório manualmente com o navegador fechado.

## Autenticação manual
- `browser login jazerei` abre Chromium headful; o usuário digita a senha diretamente no site.
- O programa não solicita, preenche, lê ou armazena senha.
- A política libera POSTs de sign-in do Firebase e de autenticação Google somente nesse fluxo (`allow_login=True`); coleta normal não usa essas exceções.
- `browser status` divulga apenas booleanos: perfil existe, cookies, localStorage, indício de login.
- Sessão expirada: `--verify` mostra `logged_in: false`; o usuário refaz o login manual.

## Classificação de requisições
- Leitura: GET/HEAD/OPTIONS de páginas, assets, API e RPCs de leitura.
- POST de leitura: `/api/search/games`, `/api/igdb/query` e RPCs `get_*`/`public_get_*`/`list_*`/`count_*`/`check_*`.
- Escrita: DELETE/PATCH/PUT sempre bloqueados; upsert/insert/update/delete/watchlist/favoritos/Playing Now bloqueados.
- POST desconhecido: bloqueado e registrado.
- GET com marca de ação (logout/delete/unwatch): bloqueado.
- Desafio Cloudflare: permitido somente em `/cdn-cgi/challenge-platform/` para concluir a verificação anti-bot.
- Não há presunção de que GET seja inofensivo nem de que POST seja escrita.

Requisições bloqueadas são abortadas no contexto do navegador e aparecem em `browser_requests.blocked_entries` com método, URL sem query e motivo. Headers e corpos não são registrados.

## Restrições read-only
- `MIGRATION_ALLOW_WRITES` permanece desabilitado; não foi ativado nesta tarefa.
- Importador real continua não implementado.
- Nenhum botão de salvar/adicionar/remover/atualizar é clicado.
- Nenhuma review, Wishlist, favorito ou Playing Now foi alterado.
- Requisições de escrita são bloqueadas em três camadas: política de rede do navegador, ausência de cliente/transporte mutável fora do gateway e gateway com transporte real desligado por padrão.
- O navegador só executa fetch same-origin de leitura.

## Exportações e integridade

- Coletas usam `data/stash/runs/<run-id>/`; não sobrescrevem o caminho legado.
- Cada run tem `export-report.json` e `manifest.json` com SHA-256/tamanho dos arquivos.
- A publicação é feita a partir de um diretório de staging; falha de HTTP/Cloudflare não cria listas vazias.
- `--overwrite` exige `--run-id` explícito e não substitui run válido se a nova coleta falhar (evidência em `*-failed-*`). Uma tentativa de overwrite que falhe não apaga o run anterior.
- Exit codes: 0 completo/dirigido/vazio válido; 1 parcial; 2 falha/alvo ausente.
- HTML bruto, quando habilitado, fica em `html/` dentro do run.
- Dados de destino continuam fora do Git (`data/jazerei/`, `data/migration/`).

## Decisões de `liked`

- `liked` é opinião do autor. A política automática solicitada em 2026-11-25 usa a nota: `≥7` → true, `≤5` → false, entre 5 e 7 → `unresolved`. Decisões automáticas usam `source: automatic`.
- Decisões humanas (`source: human`) não são sobrescritas pela política automática.
- Sem decisão (nota intermediária ou sem destino), o estado é `unresolved` e o planner bloqueia.
- Decisões vivem em `data/migration/liked-decisions.jsonl` (ignorado pelo Git), append-only.
- Cada decisão registra `source_ref`, `game_id`, `source_hash`, valor booleano, `source`, `decided_at`, `decision_id` e `supersedes`.
- `--replace` preserva a decisão anterior; decisão com hash divergente fica `stale` e não é usada.
- Review existente no destino não é atualizada; `liked` do snapshot é preservado.
- O estado `value:null` interno não significa que o backend aceita `null`; o contrato de omissão/null é desconhecido.
- Nenhuma chamada ao Já Zerei é feita para registrar decisões.

## Snapshots do Já Zerei

- `review-state` exige identidade esperada: `JAZEREI_USER_ID` ou UID local salvo pelo login, UID da sessão e `get_my_profile.id` coincidentes.
- Snapshot só é publicado com paginação comprovadamente completa e todos os `user_id` iguais ao esperado.
- Snapshots ficam em `data/jazerei/runs/<run-id>/review-state/`; anteriores não são sobrescritos.
- Manifesto com SHA-256 e tamanho; verificação equivalente à Etapa 1.
- Falha gera `capture-failure.json`, sem `review-state` válido e sem `exists:false` falso.
- Campos e URLs sensíveis são sanitizados antes da gravação; URLs com `token=`/`apikey` são limpas e `user_avatar`/e-mail/nome/tel são descartados.
- Headers Authorization/apikey são mantidos apenas em memória para o GET; não entram em snapshot, log ou manifesto.
- Nome completo, e-mail, telefone, data de nascimento e afins não são copiados do perfil; apenas o UID é usado.

## Identidade local da sessão

- O login manual descobre o UID da conta autenticada e grava
  `data/browser/session-identity.json` (diretório ignorado pelo Git).
- O arquivo contém apenas `user_id`, origem da descoberta, data e uma nota; nunca
  tokens, cookies, senha ou cabeçalhos.
- `JAZEREI_USER_ID`, quando definido no ambiente, tem precedência sobre o arquivo.
- O preflight continua comparando a identidade esperada com o UID da sessão ao
  vivo e com `get_my_profile.id`; o arquivo não substitui essa verificação.

## Aprovação de planos

- Planos e manifestos ficam em `data/migration/plans/<plan-id>/`; não são sobrescritos.
- `plan_sha256` cobre origem, destino, correspondência, decisões e ações; `manifest_sha256` cobre o manifesto; `plan_file_sha256` cobre o arquivo.
- Aprovações append-only em `data/migration/approvals.jsonl`; não há edição/remoção.
- Aprovação exige confirmação explícita do `plan_id`, proveniência completa, manifesto íntegro e validação estrutural.
- `approved:true` legado não autoriza nada.
- Alteração de origem, destino, decisão, matches, plano ou manifesto torna a aprovação `stale`/`invalid`.
- O modelo detecta alterações por hash, mas não é assinatura digital/certificação jurídica.
- Aprovação não substitui preflight; é apenas autorização documental local.

## Preflight

- Exige plano/manifesto/proveniência/aprovação atuais; não consulta o destino se pré-condição falhar.
- Reutiliza a sessão Playwright e a captura autenticada; uma autenticação e uma paginação para todos os jogos.
- Cria snapshots novos por jogo em `data/migration/preflights/<preflight-id>/`; não reutiliza snapshot antigo como estado atual.
- Compara `before` aprovado com o estado atual e não altera nenhum registro.
- Captura incompleta/identidade divergente resulta em `inconclusive`/`blocked`, nunca ausência.
- Ações não suportadas bloqueiam o resultado global.
- Resultado/manifesto append-only; não sobrescreve snapshots ou preflights anteriores.
- Preflight compatível não autoriza escrita; o gateway existe, o transporte real está desligado por padrão e foi usado em 2026-11-26 para 17 ações verificadas.

### Gateway de execução e transporte real

- `MIGRATION_ALLOW_WRITES=false` e `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=false` por padrão; ambos são necessários, nenhum é suficiente.
- `execute_action` aceita transporte simulado ou apenas `BrowserWriteTransport` real com os dois gates verdadeiros.
- Transporte real usa a sessão persistente (`context.request`), endpoint fixo `https://jazerei.com/api/reviews/upsert`, POST, sem redirect, sem retry e com timeout.
- O `Authorization: Bearer <ID token>` é capturado da sessão autenticada imediatamente antes da execução e mantido apenas em memória; não é gravado em log, manifesto ou snapshot. Sem ele, o upsert responde `401 Authentication required`.
- Payload real é validado antes do envio; `liked` booleano explícito; nenhum campo extra.
- Nenhum cookie/token é lido, serializado ou persistido pelo transporte.
- Preparação exige plano/manifesto/aprovação/preflight compatível/identidade/payload.
- Somente criação de review; update/delete/Wishlist/favoritos/Playing Now bloqueados.
- Confirmação individual append-only vinculada a plano/manifesto/preflight/ação/payload/identidade.
- Aprovação corrente e preflight posterior a ela são revalidados antes de preparar/confirmar/executar.
- Leitura imediatamente anterior e verificação posterior; timeout, conexão interrompida, HTTP 4xx/5xx, identidade divergente, captura incompleta ou review ausente após envio são `write_ambiguous` e nunca geram reenvio.
- Só falha comprovadamente anterior ao envio (`WriteNotSent`) vira `failed`.
- `execution-reconcile` transforma interrupções persistidas em ambiguidade; `execution-resolve` exige snapshot autenticado/conclusivo, nunca reenvia e mantém review ausente como ambígua.
- Enquanto houver ação ambígua, a execução fica `inconclusive` e nenhuma ação subsequente é aceita.
- Verificação posterior compara identidade, dono, jogo, nota, texto e `liked`; não aceita HTTP 2xx isolado.
- Execuções/eventos/confirmações em `data/migration/executions/<id>/`; sem sobrescrita de execuções anteriores.
- Eventos append-only preservam transições; o snapshot `execution.json` e o `manifest.json` são atualizados a cada transição. Isso é imutabilidade lógica, não resistência física a adulteração local.
- Falha de persistência interrompe o fluxo; a ação permanece `writing` até reconciliação humana.
- Nenhuma credencial em payloads, eventos, manifestos ou confirmações.
- **Limitação:** Python não isola módulos como sandbox; código externo pode importar `requests` ou acessar atributos privados. A defesa é a fronteira única, os dois gates, a validação de endpoint/payload e os testes.

### Interface de revisão (`review-ui`)

- Servidor local em `127.0.0.1`; sem autenticação, use apenas localmente.
- Não possui transporte de escrita nem chama `execution-run`.
- Atualiza `matches.json`, `liked-decisions.jsonl` e `review-overrides.jsonl` (decisões/edições locais) e registra histórico append-only.
- Pesquisa de catálogo usa somente `search_games` (POST público de leitura).
- Gerar plano usa `build_plan`/`build_bundle`; não cria aprovação, preflight ou execução.
- Categoria Stash é lida de `.games-list__category` e de `source-categories.json`; não é inferida pelo título.
- Snapshot completo da conta (`browser account-reviews`) é versionado e pode fundamentar a proveniência do plano sem criar 70 snapshots individuais.
- Ratificações humanas em lote ficam em `ratifications.jsonl` append-only; decisões automáticas sem ratificação não entram no plano.
- Aceitações editoriais ficam em `editorial-acceptances.jsonl`; não são aprovação de correspondência nem do plano.
- `verify-plan`/`approval-status` distinguem `human_individual`, `human_batch_review` e `automatic_unreviewed`.
- Categorias especiais não são autoaprovadas e geram sugestão de não migração quando não há candidato.
- Edições de nota/texto/status/progresso preservam o original e ficam em `review-overrides.jsonl`.
- Decisões ficam `stale` se a origem ou o manifesto do destino mudar.

### Superfície legada de escrita (resolvida na Etapa 8)

- Até a Etapa 7 existiam `upsert_review`, `update_profile`, `add_to_watchlist` e `remove_from_watchlist`, protegidos apenas por `MIGRATION_ALLOW_WRITES`.
- Na Etapa 8 esses helpers foram **removidos**. O cliente `JazereiClient` não possui método de escrita; `HttpClient` só aceita GET/HEAD e POST de consulta em allowlist.
- `importer.execute` também foi removido; não há segunda camada de execução.
- `allow_writes=True` não reativa helpers; o transporte real exige também `enable_real_write_transport=True` e só é construído pelo gateway.

## Dados armazenados
Podem ser gravados (todos ignorados pelo Git):
- HTML bruto opcional do Stash (`data/stash/html/`).
- Exports públicos `games.json`, `reviews.json`, `export-report.json`.
- Reviews/perfil autenticados em `data/jazerei/` quando a coleta for implementada (reviews implementadas; perfil/Wishlist ainda não).
- Lista completa da conta em `data/jazerei/runs/stage9-account-reviews-20261125/` com manifesto.
- Snapshots autenticados em `data/jazerei/runs/` com manifesto.
- Decisões de `liked` em `data/migration/liked-decisions.jsonl` (ignorado pelo Git).
- Plano e relatórios em `data/migration/`.

Não devem ser gravados:
- cookies, tokens, senhas, cabeçalhos Authorization;
- perfil Chromium fora de `data/browser/`;
- respostas privadas em relatórios públicos;
- dados desnecessários como e-mail ou data de nascimento.

## Riscos e limitações
- Cloudflare exige navegador headful no Stash; headless fica em desafio. A sessão pode expirar.
- Google pode recusar login em navegador automatizado. O programa não tenta contornar challenge do provedor; use e-mail/senha quando disponível ou login manual em ambiente não automatizado.
- Fechar a janela durante `browser login` não corrompe o perfil, mas pode interromper a confirmação; use `browser status --verify`.
- Mudanças de DOM/API quebram seletores e parsers; os relatórios expõem campos ausentes/falhas.
- Automação de navegador pode ser detectada; preferir intervalos e uso manual.
- O perfil persistente é sensível mesmo sem token em texto claro.
- Aprovação, preflight e confirmação por CLI existem, mas não executam migração; a execução real existe e já foi usada uma vez, e novas escritas exigem autorização explícita.
- O contrato do backend para omissão/`null`/default de `liked` continua desconhecido; decisão humana é obrigatória.
- `liked` foi identificado no frontend como recomendação do autor, não curtida da review; curtidas usam `review_likes`.
- O snapshot autenticado já está implementado; ainda não é consumido automaticamente pelo planner.
- A captura exige identidade esperada: `JAZEREI_USER_ID` ou UID local salvo pelo login; sem identidade, não há snapshot.
- A captura usa headers da sessão em memória; não há token em `.env` exigido para o navegador.
- `MIGRATION_DATA_DIR` movia o perfil do navegador; use `MIGRATION_BROWSER_PROFILE_DIR` se alterar o diretório de dados.
