# Настройка GitHub Actions для запуска бота на 6 часов

## ⚠️ Проблема
GitHub Actions серверы блокируются **CloudFront** при попытке подключиться к Bybit API.

**Решение:** Использовать **Mullvad VPN** (бесплатный, no-logs VPN).

---

## 🚀 Быстрая настройка (5 минут)

### Шаг 1: Добавить секреты в GitHub

Перейти: **Settings → Secrets and variables → Actions → New repository secret**

Добавить эти 6 секретов:

```
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_STRING_SESSION
TARGET_CHANNEL
BYBIT_DEMO_KEY
BYBIT_DEMO_SECRET
```

**Необязательный** секрет (если у вас есть собственный WireGuard конфиг):
```
MULLVAD_CONFIG
```

### Шаг 2: Запустить workflow

1. Перейти: **Actions**
2. Выбрать: **Crypto Bot Runner (6 hours)**
3. Нажать: **Run workflow**
4. Выбрать ветку: **main**
5. Нажать: **Run workflow**

### Шаг 3: Смотреть логи

Во время запуска:
- Смотрите **логи в реальном времени**
- Проверьте шаги:
  - ✅ Setup Mullvad VPN
  - ✅ Verify Bybit connectivity
  - ✅ Run trading bot

После завершения:
- Скачайте **bot.log** в **Artifacts**

---

## 📝 Подробная инструкция

### Получить TELEGRAM_API_ID и TELEGRAM_API_HASH

1. Перейти на https://my.telegram.org/
2. Залогиниться с номером телефона
3. Перейти в **API development tools**
4. Скопировать **api_id** и **api_hash**

### Получить TELEGRAM_STRING_SESSION

Выполнить **локально** на вашей машине:

```bash
pip install telethon
python3 << 'EOF'
from telethon import TelegramClient
import asyncio

async def get_session():
    API_ID = YOUR_API_ID  # замените на ваш api_id
    API_HASH = "YOUR_API_HASH"  # замените на ваш api_hash
    
    client = TelegramClient('session', API_ID, API_HASH)
    await client.start()
    session_string = client.session.save()
    print("TELEGRAM_STRING_SESSION:")
    print(session_string)
    await client.disconnect()

asyncio.run(get_session())
EOF
```

Скопируйте выведённую строку в `TELEGRAM_STRING_SESSION`.

### TARGET_CHANNEL

Это имя вашего Telegram канала, из которого читать сигналы:
- Если канал публичный: `@mychannel` или `mychannel`
- Если приватный: `123456789` (числовой ID канала)

Узнать ID канала:
```python
# Отправьте боту, который получит ID:
# https://t.me/username_to_id_bot
```

### BYBIT_DEMO_KEY и BYBIT_DEMO_SECRET

1. Перейти на https://testnet.bybitglobal.com/
2. Залогиниться
3. **Account → API → Create API Key** (Demo Trading)
4. Скопировать ключ и секрет в соответствующие переменные

**⚠️ ВАЖНО:** Используйте **Demo Trading API**, не live!

---

## 🔍 Мониторинг во время запуска

### Что происходит:

1. **[Setup Mullvad VPN]** (30 сек)
   - Установка VPN клиента
   - Подключение к серверу
   - IP должен измениться

2. **[Verify Bybit connectivity]** (20 сек)
   - Проверка доступа к Bybit API
   - ✅ Если успешно — идёт дальше
   - ⚠️ Если ошибка — продолжает (может работать)

3. **[Run trading bot]** (6 часов = 21600 сек)
   - Бот подключается к Telegram
   - Ожидает сигналов
   - Открывает позиции при получении сигнала
   - Логирует все действия

### Логи в реальном времени:

```
[2026-10-06 23:18:23,110] INFO: 🤖 Инициализация торгового бота...
[2026-10-06 23:18:23,115] INFO: [BYBIT] ✅ Успешно подключено к Bybit Demo API
[2026-10-06 23:18:23,230] INFO: [SUCCESS] ✅ Рынки Bybit Futures загружены
[2026-10-06 23:18:30,500] INFO: ✅ Бот подключен к Telegram
[2026-10-06 23:18:30,600] INFO: ⏱️  Запуск на 6 часов. Ожидание сигналов...
```

---

## ⏱️ Временные ограничения

- **GitHub Actions limit:** максимум 6 часов (360 минут)
- **Workflow timeout:** 370 минут (с буфером)
- **Автоматический выход:** через 6 часов (нормально, не ошибка)

Если нужен **постоянный запуск** — используйте собственный VPS.

---

## 🆘 Возможные проблемы

### "Error: CloudFront distribution is configured to block access"

✅ **Это решается VPN.** Workflow автоматически подключает Mullvad.

### "Connection refused to Telegram"

- Проверьте `TELEGRAM_STRING_SESSION` (правильна ли?)
- Попробуйте заново получить сессию

### "Bybit API 401 Unauthorized"

- Проверьте `BYBIT_DEMO_KEY` и `BYBIT_DEMO_SECRET`
- Убедитесь, что это **Demo API**, не live

### Бот не видит сообщения в канале

- Проверьте, что бот подписан на канал
- Убедитесь, что `TARGET_CHANNEL` правильный
- Проверьте формат сигнала (должен содержать `#BTC`, `ETH/USDT` и т.д.)

### "Timeout" через 6 часов

✅ **Это нормально!** GitHub Actions имеет жесткое ограничение в 6 часов.

Для более длинного запуска — используйте VPS.

---

## ✅ Проверка настройки

1. **Запустите workflow один раз:**
   ```
   Actions → Run workflow → Run
   ```

2. **Посмотрите логи** и убедитесь, что:
   - ✅ VPN подключен (IP изменился)
   - ✅ Bybit API доступен
   - ✅ Бот подключен к Telegram
   - ✅ Бот в режиме ожидания

3. **Отправьте тестовый сигнал** в ваш канал:
   ```
   Тест: #BTC покупка
   ```

4. **Проверьте логи** — бот должен распознать `#BTC`

---

## 📊 Где найти логи

После каждого запуска:

1. Перейти: **Actions → [последний run]**
2. Скачать: **Artifacts → bot-logs-XXXX**
3. Открыть: **bot.log** (полный лог всей работы)

Логи содержат:
- Время подключения
- Распознанные сигналы
- Открытые позиции (LONG/SHORT)
- Цены TP/SL
- Ошибки (если были)

---

## 🔒 Безопасность

- ✅ Все секреты **зашифрованы** GitHub
- ✅ Никогда не коммитьте `.env` в repo
- ✅ Demo API ключи безопаснее, чем live
- ✅ VPN не логирует данные (Mullvad — no-logs)

---

## 🎯 Следующие шаги

1. Добавьте секреты в GitHub
2. Запустите workflow один раз
3. Убедитесь, что логи выглядят хорошо
4. Отправьте тестовый сигнал в Telegram канал
5. Проверьте, открылась ли позиция на Bybit Demo

Готово! 🚀
