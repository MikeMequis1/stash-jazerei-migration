# Inventário do reset absoluto — 2026-11-26

Este inventário foi levantado antes da remoção. O reset apaga somente dados
operacionais. Código, testes, documentação, configuração, gates e mecanismos
de segurança não entram na remoção.

## Remover — dados operacionais

| Caminho | Conteúdo | Por que remover |
|---|---|---|
| `data/browser/` | Perfil Chromium, cookies, IndexedDB, Local Storage, Session Storage, cache, sessões, bancos SQLite internos do navegador, `session-identity.json` | Sessão/autenticação e cache de tentativas anteriores |
| `data/migration/plans/` | Planos, manifestos, hashes e aprovações vinculadas | Nenhum plano anterior deve permanecer |
| `data/migration/executions/` | Execuções, confirmações, eventos, resultados `verified`/`ready`/`failed`/`write_ambiguous`/`conflict`, reconciliações | Nenhuma tentativa anterior deve permanecer |
| `data/migration/verifications/` | Relatórios de verificação pós-escrita | Resultado de tentativa anterior |
| `data/migration/preflights/` | Preflights, snapshots de evidência e manifestos | Evidência de tentativa anterior |
| `data/migration/approvals.jsonl` | Aprovações humanas e escopos de plano | Autorização vinculada a plano antigo |
| `data/migration/stage9/ratifications.jsonl` | Ratificações em lote | Decisão vinculada a dados antigos |
| `data/migration/stage9/match-decisions.jsonl` | Histórico de decisões de correspondência | Decisão e histórico antigos |
| `data/migration/stage9/review-overrides.jsonl` | Edições humanas de reviews | Derivado de coleta antiga |
| `data/migration/stage9/editorial-acceptances.jsonl` | Aceitações editoriais | Decisão vinculada a plano antigo |
| `data/migration/stage9/source-categories.json` | Categorias derivadas da coleta antiga | Metadado derivado; será recriado pela nova coleta |
| `data/migration/stage9/*.json`, `*.md` | Filas, resumos e análises da coleta antiga | Índices e relatórios que alimentam a interface |
| `data/migration/matches.json` | Correspondências e decisões | Insumo de plano antigo |
| `data/migration/liked-decisions.jsonl` | Decisões e políticas de `liked` | Insumo de plano antigo |
| `data/migration/wizard-state.json` | Seleção ativa, execução e identidade | Estado persistente antigo |
| `data/migration/prepare-runs/` | Logs e manifestos de preparações | Tentativa operacional anterior |
| `data/migration/demolition-girl/` | Piloto Demolition Girl | Tentativa anterior |
| `data/stash/runs/` | Coletas antigas, `games.json`, `reviews.json`, relatórios e manifestos | Coleta antiga; será refeita |
| `data/jazerei/runs/` | Snapshots antigos, review-state e manifestos | Snapshot antigo; será refeito |
| `data/jazerei/reviews.json`, `data/jazerei/reviews-stage9.json` | Reviews legadas de apoio | Referência de estado antigo |
| `.pytest_cache/`, `.ruff_cache/`, `src/**/__pycache__`, `tests/__pycache__` | Caches de ferramentas | Metadados derivados; não são código |
| `data/migration/` e subdiretórios vazios | Estrutura operacional | Recriada sob demanda pela aplicação |

## Preservar — fora do escopo operacional

| Caminho | Motivo |
|---|---|
| `src/`, `tests/`, `web/` | Código, testes e interface |
| `docs/` | Documentação, incluindo este inventário e o desenho da UX |
| `README.md`, `pyproject.toml`, `requirements*.txt`, `.env.example`, `.gitignore` | Configuração e instruções |
| `reports/` | Contém apenas `.gitkeep`; diretório reservado |
| `.venv/`, `.venvclear/` | Ambientes Python; não são dados operacionais |
| Gate `MIGRATION_ALLOW_WRITES=false` | Segurança |
| Gate `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=false` | Segurança |
| Reviews e dados nas contas Stash/Já Zerei | Fora do projeto; nunca foram alvo |

## Referências indiretas

- `src/review_ui.py` contém caminhos padrão para runs antigos. Após o reset
  esses caminhos ficam ausentes; a varredura do orquestrador trata isso como
  coleta/snapshot necessários.
- `prepare_interface` foi ajustado para abrir a interface mesmo com `data/`
  vazio, sem criar plano, snapshot ou escrita.
- `data/migration/stage9/*.json|md` alimentavam resumos e índices da interface;
  serão removidos junto com o diretório.
- O perfil Chromium contém bancos LevelDB/SQLite internos; removê-lo elimina
  cookies, IndexedDB, storage e caches de autenticação de uma vez.
- Não há banco SQLite operacional do projeto fora do perfil do navegador.
- `reports/` está vazio; não há relatório operacional a remover.
