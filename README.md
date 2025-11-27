Надо указать 
```
RESOLVE_SERVICE_URL
TRACKER_QUEUE
```
TRACKER_QUEUE очередь на трекере куда будет записывать ответы агент
RESOLVE_SERVICE_URL хост агента без указания роута вида http://{ip:port, dns}


Для настройки GlitchTip со стороны сервиса нужны 
```
GLITCHTIP_API_TOKEN
GLITCHTIP_BASE_URL
```
GLITCHTIP_BASE_URL это адрес глиттипа на хосте, GLITCHTIP_API_TOKEN можно выписать Profile > Auth Tokens.

На стороне глитчтипа надо настроить нотификацию. В организации Settings > Projects > нужный проект > Add Alert Recipient, выбрать General (slack-compatible) Webhook и указать {адрес этого сервиса}/sentry/webhook

Для настройки Sentry со стороны сервиса нужны
```
SENTRY_API_TOKEN
SENTRY_BASE_URL
SENTRY_ORG
```
SENTRY_API_TOKEN выписывается в settings > developer settings > personal token. SENTRY_BASE_URL хост sentry, SENTRY_ORG слага орги н.п {roga}. 

Со строны Senry надо создать Custom Integrations (находится там же где и), указать url хука http://{хост}/sentry/webhook, включить Alert Rule Action. В issues > configure > alerts создать алерт 

1. выбрать проект и его пространство
2. when A new issue is created
3. if опционально
4. Then Send a notification via выбрать созданую интеграцию