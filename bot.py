import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import ccxt.async_support as ccxt

# Мгновенный вывод логов в консоль без задержек буферизации
os.environ["PYTHONUNBUFFERED"] = "1"

load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

# ========== ВАЛИДАЦИЯ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
def validate_env():
    """Проверяет наличие всех обязательных переменных окружения"""
    telegram_api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    telegram_api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    telegram_string_session = os.getenv("TELEGRAM_STRING_SESSION", "").strip()
    target_channel = os.getenv("TARGET_CHANNEL", "").strip()
    bybit_demo_key = os.getenv("BYBIT_DEMO_KEY", "").strip()
    bybit_demo_secret = os.getenv("BYBIT_DEMO_SECRET", "").strip()

    errors = []

    if not telegram_api_id:
        errors.append("TELEGRAM_API_ID не установлен")
    try:
        int(telegram_api_id)
    except ValueError:
        errors.append("TELEGRAM_API_ID должен быть целым числом")

    if not telegram_api_hash:
        errors.append("TELEGRAM_API_HASH не установлен")

    if not target_channel:
        errors.append("TARGET_CHANNEL не установлен (бот будет слушать все сообщения!)")

    if not bybit_demo_key:
        errors.append("BYBIT_DEMO_KEY не установлен")

    if not bybit_demo_secret:
        errors.append("BYBIT_DEMO_SECRET не установлен")

    if errors:
        logger.error("=" * 70)
        logger.error("❌ ОШИБКА КОНФИГУРАЦИИ:")
        for error in errors:
            logger.error(f"  • {error}")
        logger.error("=" * 70)
        raise RuntimeError("Не хватает обязательных переменных окружения")

    return {
        'telegram_api_id': int(telegram_api_id),
        'telegram_api_hash': telegram_api_hash,
        'telegram_string_session': telegram_string_session if telegram_string_session else None,
        'target_channel': target_channel,
        'bybit_demo_key': bybit_demo_key,
        'bybit_demo_secret': bybit_demo_secret,
    }

try:
    CONFIG = validate_env()
    TELEGRAM_API_ID = CONFIG['telegram_api_id']
    TELEGRAM_API_HASH = CONFIG['telegram_api_hash']
    TELEGRAM_STRING_SESSION = CONFIG['telegram_string_session']
    TARGET_CHANNEL = CONFIG['target_channel']
    BYBIT_DEMO_KEY = CONFIG['bybit_demo_key']
    BYBIT_DEMO_SECRET = CONFIG['bybit_demo_secret']
except RuntimeError as e:
    logger.critical(str(e))
    exit(1)

# ========== ГЛОБАЛЬНОЕ СОСТОЯНИЕ ==========
exchange = None
client = None

# Очередь обработки сигналов (предотвращает параллельные заказы на один актив)
processing_symbols = {}  # {'BTC/USDT:USDT': datetime}
active_positions = set()  # {'BTC/USDT:USDT', ...}
signal_lock = asyncio.Lock()  # Синхронизирует доступ к активным позициям
POSITION_COOLDOWN_SECONDS = 300  # Защита от дубль-сигналов на 5 минут

# ========== КРИТИЧЕСКИ ВАЖНЫЙ БЛОК ПОДКЛЮЧЕНИЯ К BYBIT ==========
async def init_bybit(max_retries: int = 3, retry_delay: float = 2.0):
    """
    Инициализация Bybit с защитой от сетевых ошибок и автоматическими ретраями
    """
    global exchange

    attempt = 0
    current_delay = retry_delay

    while attempt < max_retries:
        try:
            logger.info(f"[BYBIT] Попытка подключения {attempt + 1}/{max_retries}...")

            exchange = ccxt.bybit({
                'apiKey': BYBIT_DEMO_KEY,
                'secret': BYBIT_DEMO_SECRET,
                'enableRateLimit': True,
                'timeout': 10000,
                'options': {
                    'defaultType': 'linear',
                    'defaultSubType': 'linear',
                    'defaultMarginMode': 'cross',
                    'recvWindow': 5000,
                }
            })

            try:
                await exchange.fetch_ticker('BTC/USDT:USDT')
                logger.info("[BYBIT] ✅ Тестовый запрос успешен, соединение проверено")
            except Exception as e:
                logger.warning(f"[BYBIT] Тестовый запрос failed: {e}, но инициализация продолжается")

            if hasattr(exchange, 'enable_demo_trading'):
                try:
                    exchange.enable_demo_trading(True)
                    logger.info("[BYBIT] Demo Trading режим активирован")
                except Exception as e:
                    logger.warning(f"[BYBIT] Не удалось включить Demo Trading: {e}")

            logger.info("[BYBIT] ✅ Успешно подключено к Bybit Demo API")
            return True

        except Exception as e:
            attempt += 1
            logger.error(f"[BYBIT] ❌ Попытка {attempt} не удалась: {e}")

            if attempt < max_retries:
                logger.info(f"[BYBIT] ⏳ Ожидание {current_delay:.1f}с перед следующей попыткой...")
                await asyncio.sleep(current_delay)
                current_delay *= 1.5
            else:
                logger.error("[BYBIT] ❌ Все попытки подключения исчерпаны")
                return False

    return False

# ================================================================

# Инициализация Telethon
if TELEGRAM_STRING_SESSION:
    client = TelegramClient(
        StringSession(TELEGRAM_STRING_SESSION),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        auto_reconnect=True,
        connection_retries=None
    )
else:
    client = TelegramClient('user_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)


def parse_asset(text: str):
    """
    Извлекает наименование торговой пары из текста сигналов
    Возвращает формат: 'BTC/USDT:USDT' (для Bybit linear perpetuals)
    """
    if not text:
        return None

    ignored_words = {
        'BUY', 'SELL', 'LONG', 'SHORT', 'TP', 'SL', 'USDT',
        'SIGNAL', 'SPOT', 'FUTURES', 'LIQUIDATED', 'BINANCE', 'PRIMARY',
        'AND', 'OR', 'FOR', 'THE', 'IS', 'AT', 'TO', 'ON'
    }

    hashtag_match = re.search(r'#([A-Z0-9]{2,15})(?:/USDT|USDT)?', text.upper())
    if hashtag_match:
        raw_symbol = hashtag_match.group(1)
        if raw_symbol not in ignored_words and len(raw_symbol) >= 2:
            return f"{raw_symbol}/USDT:USDT"

    pair_match = re.search(r'\b([A-Z0-9]{2,15})(?:/USDT|USDT)\b', text.upper())
    if pair_match:
        raw_symbol = pair_match.group(1)
        if raw_symbol not in ignored_words and len(raw_symbol) >= 2:
            return f"{raw_symbol}/USDT:USDT"

    return None


async def validate_symbol(symbol: str) -> bool:
    """Проверяет валидность символа в Bybit маркетах"""
    try:
        if symbol not in exchange.markets:
            logger.warning(f"[VALIDATE] ⚠️  Символ {symbol} не найден в Bybit Markets")
            return False

        market = exchange.market(symbol)
        if market.get('active') is False:
            logger.warning(f"[VALIDATE] ⚠️  Символ {symbol} неактивен на бирже")
            return False

        return True
    except Exception as e:
        logger.error(f"[VALIDATE] ❌ Ошибка проверки {symbol}: {e}")
        return False


async def setup_symbol_config(symbol: str) -> int:
    """Автоматически определяет и выставляет МАКСИМАЛЬНОЕ доступное плечо"""
    try:
        market = exchange.market(symbol)
        max_leverage = market.get('limits', {}).get('leverage', {}).get('max', 50)
        max_leverage = int(max_leverage) if max_leverage else 50

        try:
            await exchange.set_margin_mode('cross', symbol, params={'tradeMode': 0})
            logger.info(f"[MARGIN] Кросс-маржа установлена для {symbol}")
        except Exception as e:
            err_str = str(e).lower()
            if "already" in err_str or "cross" in err_str:
                logger.debug(f"[MARGIN] Кросс-маржа уже активна для {symbol}")
            else:
                logger.warning(f"[MARGIN] Ошибка при установке маржи {symbol}: {e}")

        try:
            await exchange.set_leverage(max_leverage, symbol)
            logger.info(f"[LEVERAGE MAX] ✅ Плечо {max_leverage}x для {symbol}")
        except Exception as e:
            err_str = str(e).lower()
            if "110043" in err_str or "11043" in err_str or "leverage not modified" in err_str:
                logger.debug(f"[LEVERAGE MAX] Плечо уже установлено ({max_leverage}x)")
            else:
                logger.warning(f"[LEVERAGE WARNING] Ошибка для {symbol}: {e}")

        return max_leverage

    except Exception as e:
        logger.error(f"[ERROR] Не удалось прочитать конфиг {symbol}: {e}. Возврат 10x")
        return 10


async def open_dual_positions(symbol: str, margin_usdt: float = 10.0):
    """Открывает Long и Short позиции с МАКСИМАЛЬНЫМ плечом"""
    async with signal_lock:
        if symbol in processing_symbols:
            last_time = processing_symbols[symbol]
            if datetime.now() - last_time < timedelta(seconds=POSITION_COOLDOWN_SECONDS):
                logger.warning(
                    f"[SKIP] {symbol} уже обрабатывается (защита от дубль-сигналов). "
                    f"Следующий сигнал возможен через {POSITION_COOLDOWN_SECONDS}с"
                )
                return

        processing_symbols[symbol] = datetime.now()
        active_positions.add(symbol)

    try:
        if not await validate_symbol(symbol):
            logger.error(f"[EXECUTION] ❌ Символ {symbol} не валиден")
            return

        leverage = await setup_symbol_config(symbol)

        try:
            ticker = await exchange.fetch_ticker(symbol)
            entry_price = ticker.get('last')
            if not entry_price:
                logger.error(f"[EXECUTION] ❌ Не удалось получить цену для {symbol}")
                return
        except Exception as e:
            logger.error(f"[EXECUTION] ❌ Ошибка fetch_ticker {symbol}: {e}")
            return

        position_value = margin_usdt * leverage
        raw_quantity = position_value / entry_price
        quantity = float(exchange.amount_to_precision(symbol, raw_quantity))

        min_amount = exchange.market(symbol).get('limits', {}).get('amount', {}).get('min', 0)
        if quantity < min_amount:
            logger.warning(
                f"[SKIP] Объём {quantity} < минимума {min_amount} для {symbol}. "
                f"Увеличьте margin_usdt"
            )
            return

        tp_percent = 1.50 / leverage
        sl_percent = 1.00 / leverage

        long_tp = entry_price * (1 + tp_percent)
        long_sl = entry_price * (1 - sl_percent)

        short_tp = entry_price * (1 - tp_percent)
        short_sl = entry_price * (1 + sl_percent)

        logger.info(
            f"\n[EXECUTION] {symbol} | "
            f"Цена: {entry_price:.4f} USDT | "
            f"Маржа: ${margin_usdt} | "
            f"Плечо: {leverage}x | "
            f"Объём: {quantity:.6f}"
        )

        try:
            long_order = await exchange.create_order(
                symbol=symbol,
                type='market',
                side='buy',
                amount=quantity,
                params={
                    'takeProfit': float(exchange.price_to_precision(symbol, long_tp)),
                    'stopLoss': float(exchange.price_to_precision(symbol, long_sl)),
                    'positionIdx': 1
                }
            )
            logger.info(
                f"[LONG OPENED] ✅ "
                f"Order ID: {long_order.get('id')} | "
                f"TP: {long_tp:.4f} | SL: {long_sl:.4f}"
            )
        except Exception as e:
            logger.error(f"[LONG FAILED] ❌ Ошибка открытия long для {symbol}: {e}")
            return

        try:
            short_order = await exchange.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=quantity,
                params={
                    'takeProfit': float(exchange.price_to_precision(symbol, short_tp)),
                    'stopLoss': float(exchange.price_to_precision(symbol, short_sl)),
                    'positionIdx': 2
                }
            )
            logger.info(
                f"[SHORT OPENED] ✅ "
                f"Order ID: {short_order.get('id')} | "
                f"TP: {short_tp:.4f} | SL: {short_sl:.4f}"
            )
        except Exception as e:
            logger.error(f"[SHORT FAILED] ❌ Ошибка открытия short для {symbol}: {e}")
            return

    except Exception as e:
        logger.error(f"[ERROR] Ошибка при обработке {symbol}: {e}", exc_info=True)
    finally:
        async with signal_lock:
            active_positions.discard(symbol)


@client.on(events.NewMessage(chats=TARGET_CHANNEL))
async def handle_new_message(event):
    """Обработчик новых сообщений из TARGET_CHANNEL"""
    try:
        message_text = event.raw_text
        logger.info(f"\n[NEW SIGNAL] Сообщение получено: {message_text[:100]}...")

        symbol = parse_asset(message_text)
        if symbol:
            logger.info(f"[PARSED ASSET] ✅ Распознан актив: {symbol}")
            asyncio.create_task(open_dual_positions(symbol, margin_usdt=10.0))
        else:
            logger.debug("[SKIP] Актив в сообщении не распознан")
    except Exception as e:
        logger.error(f"[HANDLER ERROR] Ошибка обработки сообщения: {e}", exc_info=True)


async def load_markets_with_retry(max_retries: int = 3, retry_delay: float = 2.0) -> bool:
    """Загрузка маркетов с автоматическими ретраями"""
    attempt = 0
    current_delay = retry_delay

    while attempt < max_retries:
        try:
            logger.info(f"[MARKETS] Загрузка маркетов (попытка {attempt + 1}/{max_retries})...")
            await exchange.load_markets(params={'type': 'linear'})

            market_count = len(exchange.markets)
            logger.info(f"[MARKETS] ✅ Успешно загружено {market_count} маркетов")
            return True

        except Exception as e:
            attempt += 1
            logger.error(f"[MARKETS] ❌ Попытка {attempt} не удалась: {e}")

            if attempt < max_retries:
                logger.info(f"[MARKETS] ⏳ Ожидание {current_delay:.1f}с перед следующей попыткой...")
                await asyncio.sleep(current_delay)
                current_delay *= 1.5
            else:
                logger.error("[MARKETS] ❌ Все попытки загрузки маркетов исчерпаны")
                return False

    return False


async def cleanup_resources():
    """Корректно закрывает все открытые соединения и ресурсы"""
    global exchange, client
    
    logger.info("=" * 70)
    logger.info("🔌 Выполняется выключение бота...")
    logger.info("=" * 70)

    # Закрытие CCXT exchange
    if exchange:
        try:
            await exchange.close()
            logger.info("[SHUTDOWN] ✅ Сессия CCXT закрыта корректно")
        except Exception as e:
            logger.warning(f"[SHUTDOWN] ⚠️  Ошибка при закрытии exchange: {e}")
        finally:
            exchange = None

    # Отключение Telethon клиента
    if client:
        try:
            await client.disconnect()
            logger.info("[SHUTDOWN] ✅ Telethon отключен корректно")
        except Exception as e:
            logger.warning(f"[SHUTDOWN] ⚠️  Ошибка при отключении Telethon: {e}")
        finally:
            client = None

    logger.info("=" * 70)
    logger.info("⏹️  Бот остановлен")


async def main():
    """Основная функция с улучшенной обработкой ошибок"""
    global client, exchange

    logger.info("=" * 70)
    logger.info("🤖 Инициализация торгового бота...")
    logger.info("=" * 70)
    logger.info(f"📡 Канал: {TARGET_CHANNEL}")
    logger.info(f"🪙 Bybit Demo API: {'активен' if BYBIT_DEMO_KEY else 'не настроен'}")
    logger.info("=" * 70)

    try:
        if not await init_bybit(max_retries=3, retry_delay=2.0):
            logger.error("❌ Не удалось подключиться к Bybit. Выход.")
            return

        if not await load_markets_with_retry(max_retries=3, retry_delay=2.0):
            logger.error("❌ Не удалось загрузить маркеты. Выход.")
            return

        await client.start()
        logger.info("✅ Бот подключен к Telegram")
        logger.info(f"⏱️  Бот запущан. Ожидание сигналов из {TARGET_CHANNEL}...")
        logger.info("=" * 70)

        # Запуск основного цикла Telegram клиента
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"[ERROR] Критическая ошибка: {e}", exc_info=True)
    finally:
        # Гарантированное закрытие всех ресурсов в любом случае
        await cleanup_resources()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n⏹️  Бот остановлен вручную (Ctrl+C)")
    except SystemExit:
        logger.info("⏹️  Бот остановлен (SystemExit)")
    except Exception as e:
        logger.critical(f"[FATAL] Необработанная ошибка: {e}", exc_info=True)
