import os
import re
import asyncio
import logging
from datetime import datetime
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

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", 0))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")

BYBIT_DEMO_KEY = os.getenv("BYBIT_DEMO_KEY")
BYBIT_DEMO_SECRET = os.getenv("BYBIT_DEMO_SECRET")

# ========== КРИТИЧЕСКИ ВАЖНЫЙ БЛОК ПОДКЛЮЧЕНИЯ К BYBIT ==========
# Этот блок должен оставаться без изменений! Работает через любой VPN.
exchange = None

async def init_bybit():
    """
    Инициализация Bybit с защитой от сетевых ошибок
    """
    global exchange
    try:
        exchange = ccxt.bybit({
            'apiKey': BYBIT_DEMO_KEY,
            'secret': BYBIT_DEMO_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'defaultMarginMode': 'cross',
            }
        })
        
        # Активация режима Bybit V5 Demo Trading
        if hasattr(exchange, 'enable_demo_trading'):
            exchange.enable_demo_trading(True)
        
        logger.info("[BYBIT] ✅ Успешно подключено к Bybit Demo API")
        return True
    except Exception as e:
        logger.error(f"[BYBIT] ❌ Ошибка подключения: {e}")
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
    """
    if not text:
        return None

    ignored_words = {
        'BUY', 'SELL', 'LONG', 'SHORT', 'TP', 'SL', 'USDT',
        'SIGNAL', 'SPOT', 'FUTURES', 'LIQUIDATED', 'BINANCE', 'PRIMARY'
    }

    # 1. Поиск по хэштегу (#BTC, #ETH, #SOL)
    hashtag_match = re.search(r'#([A-Z0-9]{2,15})(?:/USDT|USDT)?', text.upper())
    if hashtag_match:
        raw_symbol = hashtag_match.group(1)
        if raw_symbol not in ignored_words:
            return f"{raw_symbol}/USDT:USDT"

    # 2. Поиск по торговым парам (BTC/USDT, ETHUSDT)
    pair_match = re.search(r'\b([A-Z0-9]{2,15})(?:/USDT|USDT)\b', text.upper())
    if pair_match:
        raw_symbol = pair_match.group(1)
        if raw_symbol not in ignored_words:
            return f"{raw_symbol}/USDT:USDT"

    return None


async def setup_symbol_config(symbol: str) -> int:
    """
    Автоматически определяет и выставляет МАКСИМАЛЬНОЕ доступное плечо
    """
    try:
        market = exchange.market(symbol)

        # Считываем максимум биржи для актива
        max_leverage = market.get('limits', {}).get('leverage', {}).get('max', 100)
        max_leverage = int(max_leverage) if max_leverage else 100

        # Включаем режим Кросс-маржи
        try:
            await exchange.set_margin_mode('cross', symbol, params={'tradeMode': 0})
        except Exception:
            pass  # Кросс-маржа уже может быть активна

        # Выставляем МАКСИМАЛЬНОЕ плечо
        try:
            await exchange.set_leverage(max_leverage, symbol)
            logger.info(f"[LEVERAGE MAX] Плечо {max_leverage}x для {symbol} ✅")
        except Exception as e:
            err_str = str(e).lower()
            if "110043" in err_str or "11043" in err_str or "leverage not modified" in err_str:
                logger.info(f"[LEVERAGE MAX] Плечо уже установлено ({max_leverage}x)")
            else:
                logger.warning(f"[LEVERAGE WARNING] Ошибка для {symbol}: {e}")

        return max_leverage

    except Exception as e:
        logger.error(f"[ERROR] Не удалось прочитать {symbol}: {e}. Возврат 50x")
        return 50


async def open_dual_positions(symbol: str, margin_usdt: float = 10.0):
    """
    Открывает Long и Short позиции с МАКСИМАЛЬНЫМ плечом
    """
    try:
        if symbol not in exchange.markets:
            logger.error(f"[ERROR] Символ {symbol} не найден в Bybit Demo")
            return

        leverage = await setup_symbol_config(symbol)

        ticker = await exchange.fetch_ticker(symbol)
        entry_price = ticker['last']

        # Расчет объема через плечо
        position_value = margin_usdt * leverage
        raw_quantity = position_value / entry_price
        quantity = float(exchange.amount_to_precision(symbol, raw_quantity))

        # Проверка минимального размера ордера
        min_amount = exchange.market(symbol).get('limits', {}).get('amount', {}).get('min', 0)
        if quantity < min_amount:
            logger.warning(f"[SKIP] Объём {quantity} < минимума {min_amount} для {symbol}")
            return

        # Расчет TP и SL
        tp_percent = 1.50 / leverage
        sl_percent = 1.00 / leverage

        long_tp = entry_price * (1 + tp_percent)
        long_sl = entry_price * (1 - sl_percent)

        short_tp = entry_price * (1 - tp_percent)
        short_sl = entry_price * (1 + sl_percent)

        logger.info(f"\n[EXECUTION] {symbol} | Цена: {entry_price} | Маржа: ${margin_usdt} | Плечо: {leverage}x | Объём: {quantity}")

        # LONG Position
        await exchange.create_order(
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
        logger.info(f"[LONG OPENED] TP: {long_tp:.4f} | SL: {long_sl:.4f} ✅")

        # SHORT Position
        await exchange.create_order(
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
        logger.info(f"[SHORT OPENED] TP: {short_tp:.4f} | SL: {short_sl:.4f} ✅")

    except Exception as e:
        logger.error(f"[ERROR] Ошибка ордеров {symbol}: {e}")


@client.on(events.NewMessage(chats=TARGET_CHANNEL if TARGET_CHANNEL else None))
async def handle_new_message(event):
    message_text = event.raw_text
    logger.info(f"\n[NEW SIGNAL] {message_text}")

    symbol = parse_asset(message_text)
    if symbol:
        logger.info(f"[PARSED ASSET] ✅ {symbol}")
        asyncio.create_task(open_dual_positions(symbol, margin_usdt=10.0))
    else:
        logger.info("[SKIP] Актив не распознан")


async def main():
    logger.info("=" * 60)
    logger.info("🤖 Инициализация торгового бота...")
    logger.info("=" * 60)

    # Инициализация Bybit
    if not await init_bybit():
        logger.error("❌ Не удалось подключиться к Bybit. Выход.")
        return

    try:
        # Загружаем рынки в кэш
        await exchange.load_markets(params={'type': 'future'})
        logger.info("[SUCCESS] ✅ Рынки Bybit Futures загружены")
    except Exception as e:
        logger.error(f"[ERROR] Ошибка загрузки рынков: {e}")
        return

    try:
        await client.start()
        logger.info("✅ Бот подключен к Telegram")
        logger.info("⏱️  Запуск на 6 часов. Ожидание сигналов...")
        logger.info("=" * 60)
        
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"[ERROR] Telegram ошибка: {e}")
    finally:
        try:
            await exchange.close()
            logger.info("[SHUTDOWN] ✅ Сессия CCXT закрыта")
        except:
            pass


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("\n⏹️  Бот остановлен вручную")
    except Exception as e:
        logger.error(f"[FATAL] {e}")
