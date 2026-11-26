# Segurança e privacidade

Nunca versionar access_token, id_token, refresh_token, cookies, Authorization, API keys, .env real, exports reais ou dumps do DevTools com dados de sessão.

Usar .env local ou armazenamento seguro do sistema. Placeholders provisórios:
```dotenv
JAZEREI_FIREBASE_API_KEY=
JAZEREI_USER_ID=
JAZEREI_ID_TOKEN=
JAZEREI_REFRESH_TOKEN=
```
Esses nomes não são contrato final de autenticação.

Regras:
- Modo padrão read-only.
- Escrita exige flag explícita e plano aprovado.
- Mostrar ações destrutivas antes de executar.
- Nunca registrar tokens em logs/exceções.
- Sanitizar fixtures e relatórios.
- Evitar coletar e-mail, data de nascimento e dados desnecessários.
- Respeitar termos, controles de acesso e limites.
