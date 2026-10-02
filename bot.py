import os
import re
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events
import ccxt.async_support as ccxt

load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", 0))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")

BYBIT_DEMO_KEY = os.getenv("BYBIT_DEMO_KEY")
BYBIT_DEMO_SECRET = os.getenv("BYBIT_DEMO_SECRET")

# Инициализация Bybit Demo (Futures)
exchange = ccxt.bybit({
    'apiKey': BYBIT_DEMO_KEY,
    'secret': BYBIT_DEMO_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future',
        'defaultMarginMode': 'cross',  # Устанавливаем режим кросс-маржи по умолчанию
    }
})
exchange.set_sandbox_mode(True)

client = TelegramClient('user_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)

def parse_asset(text: str):
    """
    Извлекает только наименование актива из текста сообщения.
    """
    pattern = r'\b([A-Z0-9]{2,10})(?:USDT)?\b'
    match = re.search(pattern, text.upper())
    
    if match:
        raw_symbol = match.group(1)
        ignored_words = {'BUY', 'SELL', 'LONG', 'SHORT', 'TP', 'SL', 'USDT', 'SIGNAL'}
        if raw_symbol in ignored_words:
            return None
        
        formatted_symbol = f"{raw_symbol}/USDT:USDT"
        return formatted_symbol
    return None

async def setup_symbol_config(symbol: str):
    """
    Устанавливает режим КРОСС-маржи и максимальное плечо для торговой пары.
    """
    try:
        markets = await exchange.load_markets()
        market = markets.get(symbol)
        
        # 1. Переключение на Кросс-маржу (Cross Margin Mode: 0 - Cross, 1 - Isolated на Bybit)
        try:
            await exchange.set_margin_mode('cross', symbol, params={'tradeMode': 0})
            print(f"[MARGIN] Режим Кросс-маржи успешно установлен для {symbol}")
        except Exception as e:
            print(f"[MARGIN NOTE] Уведомление по кросс-марже ({symbol}): {e}")

        # 2. Получение максимального плеча
        max_leverage = market.get('limits', {}).get('leverage', {}).get('max', 50) if market else 50
        
        # 3. Установка максимального плеча
        await exchange.set_leverage(int(max_leverage), symbol)
        print(f"[LEVERAGE] Установлено максимальное плечо: {max_leverage}x для {symbol}")
        
        return int(max_leverage)
    except Exception as e:
        print(f"[WARNING] Ошибка настройки пары {symbol}: {e}. Используем плечо 20x по умолчанию.")
        return 20

async def open_dual_positions(symbol: str, margin_usdt: float = 10.0):
    """
    Открывает одновременно ордер в Long и в Short в режиме Kросс-маржи
    с максимальным плечом, маржой 10$ и установкой TP (+150% ROI) / SL (-100% ROI).
    """
    try:
        leverage = await setup_symbol_config(symbol)
        
        ticker = await exchange.fetch_ticker(symbol)
        entry_price = ticker['last']
        
        # Общий позиционный объем (Маржа * Плечо)
        position_value = margin_usdt * leverage
        quantity = position_value / entry_price
        
        # Расчет TP (150% ROI) и SL (100% ROI)
        tp_percent = 1.50 / leverage
        sl_percent = 1.00 / leverage
        
        # Цены для Long
        long_tp = entry_price * (1 + tp_percent)
        long_sl = entry_price * (1 - sl_percent)
        
        # Цены для Short
        short_tp = entry_price * (1 - tp_percent)
        short_sl = entry_price * (1 + sl_percent)

        print(f"\n[EXECUTION] Вход в позиции (Кросс-маржа) по {symbol} | Цена: {entry_price} | Размер маржи: {margin_usdt}$ | Плечо: {leverage}x")

        # 1. Ордер в LONG
        long_order = await exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=quantity,
            params={
                'takeProfit': exchange.price_to_precision(symbol, long_tp),
                'stopLoss': exchange.price_to_precision(symbol, long_sl),
                'positionIdx': 1  # Для Bybit Hedge Mode: 1 - Long
            }
        )
        print(f"[LONG OPENED] TP: {long_tp:.4f} (+150% ROI) | SL: {long_sl:.4f} (-100% ROI)")

        # 2. Ордер в SHORT
        short_order = await exchange.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=quantity,
            params={
                'takeProfit': exchange.price_to_precision(symbol, short_tp),
                'stopLoss': exchange.price_to_precision(symbol, short_sl),
                'positionIdx': 2  # Для Bybit Hedge Mode: 2 - Short
            }
        )
        print(f"[SHORT OPENED] TP: {short_tp:.4f} (+150% ROI) | SL: {short_sl:.4f} (-100% ROI)")

    except Exception as e:
        print(f"[ERROR] Ошибка при исполнении ордеров: {e}")

@client.on(events.NewMessage(chats=TARGET_CHANNEL if TARGET_CHANNEL else None))
async def handle_new_message(event):
    message_text = event.raw_text
    print(f"\n[NEW SIGNAL] Сообщение: {message_text}")
    
    symbol = parse_asset(message_text)
    if symbol:
        print(f"[PARSED ASSET] Обнаружен актив: {symbol}")
        await open_dual_positions(symbol, margin_usdt=10.0)
    else:
        print("[SKIP] Актив в сообщении не найден.")

async def main():
    print("Запуск бота...")
    await client.start()
    print("Бот запущен и мониторит сигналы (Режим Кросс-маржи)...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        asyncio.run(exchange.close())
