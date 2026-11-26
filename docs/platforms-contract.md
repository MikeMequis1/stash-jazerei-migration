# `platforms` no upsert de avaliação — investigação somente leitura

**Data:** 2026-11-26  
**Escopo:** contrato observado no frontend, no gateway e nos dados de origem. Nenhuma requisição de escrita foi feita.

## Fatos confirmados

- O frontend atual (`JogoDetalhe`) envia `platforms` no corpo de `POST /api/reviews/upsert`:
  `{"game_id": <int>, "rating": <number>, "text": <string>, "liked": <bool>, "platforms": [<string>, ...]}`.
- O formulário inicia `platforms: []` e alterna múltiplas plataformas; a seleção é um array de nomes.
- Linhas reais de `reviews` observadas em leitura contêm arrays como `["PC", "Xbox"]`, confirmando que o campo é armazenado como lista de nomes.
- O gateway atual (`src/migration/execution.py`) monta e valida exatamente quatro campos: `game_id`, `rating`, `text`, `liked`. Ele não envia `platforms`.
- A documentação anterior (`docs/jazerei-api.md`) registrava o payload de quatro campos; o frontend atual mostra cinco.
- Na origem Stash, `games.json` tem `platform: null` em **1.114/1.114** jogos. `reviews.json` não possui campo de plataforma. Não há dado de plataforma a migrar sem uma nova coleta.

## Desconhecidos

- Se `platforms` é obrigatório, opcional, ou aceita omissão no backend.
- Se `null` e `[]` são equivalentes para o backend.
- Se o backend valida nomes, quantidade ou duplicidade.
- Se a ausência do campo altera a resposta ou o registro de outra forma.
- Se o endpoint aceita o payload antigo de quatro campos sem erro.

Esses pontos não podem ser resolvidos por leitura. Testá-los exige uma escrita real, explicitamente fora desta etapa.

## Representação adotada

- **Não inventar plataforma.** Como a origem não possui o dado e o contrato é desconhecido, o gateway continua sem `platforms`.
- `rating_only` e `written_review` compartilham o mesmo payload de quatro campos; texto vazio é o único differencial.
- O campo não deve ser preenchido com `[]` nem `null` por conveniência: isso afirmaria um valor não observado.
- Se o contrato futuro exigir `platforms`, a decisão precisa ser explícita e documentada; a origem ainda precisará de coleta complementar.

## Risco atual

O frontend envia cinco campos; o gateway envia quatro. Se o backend passou a exigir `platforms`, a primeira escrita real pode falhar com 4xx. Isso deve ser resolvido com um teste controlado e autorizado, nunca por tentativa automática, e não nesta etapa.
