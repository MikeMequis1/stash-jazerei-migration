# Stash → Já Zerei Migration

Ferramenta local para exportar jogos e reviews do Stash, relacioná-los ao catálogo do Já Zerei e preparar uma migração revisável e auditável.

> **Estado:** descoberta/documentação. Implementação ainda não iniciada. Escritas desabilitadas por padrão.

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

## Princípios
1. Leitura primeiro; escrita desabilitada por padrão.
2. Nunca versionar tokens, cookies, chaves ou exports reais.
3. Não sobrescrever reviews sem aprovação.
4. Não truncar silenciosamente Playing Now.
5. Preservar dados originais e trilha de auditoria.

## Status
- [x] Descoberta inicial do Stash
- [x] Identificação das operações de reviews, Wishlist, perfil e catálogo no Já Zerei
- [ ] Exportador Stash
- [ ] Cliente Já Zerei read-only
- [ ] Correspondência e revisão de catálogo
- [ ] Plano de migração/aprovação
- [ ] Importação controlada e verificação

Endpoints documentados são observações empíricas do DevTools, não documentação oficial; podem mudar.
