# REDLINE 3.0

## Основные обновления
- Переработан фирменный стиль, типографика, карточки и навигация.
- Добавлена адаптация для узких экранов и нижняя мобильная панель.
- Каталог расширен до 79 автомобилей и 42 марок.
- Поиск поддерживает кириллицу, латиницу, транслитерацию, разговорные названия, синонимы и небольшие опечатки.
- Добавлены подсказки, сортировка и фильтры по сегментам и маркам.
- Добавлены подробные карточки, похожие модели, избранное и сравнение до трёх машин.
- Добавлены SEO-страницы всех марок, Open Graph, Schema.org SearchAction, sitemap-шаблон и robots.txt.
- Добавлен PWA manifest и Service Worker с сетевой проверкой страниц и без кеширования API объявлений.

## Ограничения
- Встроенный автоматический сбор объявлений не подключён: для него нужен разрешённый API/партнёрская выгрузка и сервер.
- В локальном проекте поиск по объявлениям создаёт поисковые ссылки на внешние площадки; результат и статус проверяются на источнике.
- Поисковая система не знает все существующие комплектации и модели мира. Для добавления новых машин редактируется `assets/cars.js`.
- Для публичной индексации нужны домен, HTTPS, хостинг и отправка sitemap в поисковые панели.

## REDLINE Live Listings Connector — 6 November 2026
- Added a Python standard-library server with SQLite storage and a 10-minute feed scheduler.
- Added JSON partner-feed connectors, health/source endpoints, and token-protected manual sync.
- Added a live listings UI, provider status badges, refresh/search, region filtering, and sorting by source currency.
- Enforced 15-minute source verification, active-only status, expiry filtering, HTTPS allowlists, and per-field photo/phone reuse permissions.
- Added provider configuration templates, Windows launcher, partner request template, integration status and tests.
- All third-party providers remain disabled until authorized API/JSON feeds and reuse permissions are obtained.


## REDLINE 3.2 — готовность к публикации
- Добавлена конфигурация Render Blueprint (`render.yaml`) для Python Web Service в Singapore с постоянным диском SQLite.
- Сервер использует платформенную переменную `PORT` и публичный bind `0.0.0.0`.
- Добавлены HTTP security headers и HSTS при HTTPS за reverse proxy.
- `robots.txt`, `sitemap.xml` и canonical URL формируются под публичный домен.
- Добавлена GitHub Actions проверка Python, unit-тестов и JavaScript.
- Добавлена подробная русская инструкция по GitHub → Render → DNS → HTTPS.
