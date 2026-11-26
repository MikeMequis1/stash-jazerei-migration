# Arquitetura proposta

Stash é origem; Já Zerei é destino. O formato intermediário é independente. Exports originais são imutáveis. Correspondências são explícitas e auditáveis. Toda escrita exige aprovação e verificação posterior.

## Fluxo
```
Stash → coletor/parsers → export bruto
Já Zerei (catálogo + estado atual) → correspondência
→ plano de migração → aprovação humana
→ importador controlado → verificação pós-escrita
```

## Módulos
```
src/
  stash/{client,pagination,games,reviews,parser}.py
  jazerei/{auth,client,catalog,reviews,watchlist,profile}.py
  migration/{normalize,matcher,planner,validator,importer,verifier}.py
  models/{source,normalized,destination}.py
```

## Modelo intermediário
GameRecord: origem, id/slug, título, capa, ano, status bruto, data exibida bruta, nota opcional, plataforma, URL e dados originais.

ReviewRecord: slug do jogo, texto, nota, data, status/progresso, contagens, texto da listagem/detalhe, URL e dados originais.

GameMatch: slug de origem, id/slug/nome do destino, confiança, razões e decisão (candidato, aprovado, rejeitado, revisão manual).

MigrationAction: tipo, registro de origem, game_id de destino, before/after, aprovação, execução e verificação.

## Correspondência e conflitos
Usar pesquisa do catálogo; avaliar slug, nome normalizado, ano, desenvolvedora, plataformas e contexto de versão/remake/port. Nunca escolher apenas o primeiro resultado. Manter jogos sem correspondência; não substituir review existente sem aprovação; distinguir nota com texto vazio de ausência de review; não mapear status sem tabela validada.
