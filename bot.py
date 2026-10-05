import os
import re
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import ccxt.async_support as ccxt

load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", 0))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")

BYBIT_DEMO_KEY = os.getenv("BYBIT_DEMO_KEY")
BYBIT_DEMO_SECRET = os.getenv("BYBIT_DEMO_SECRET")

# Инициализация Bybit c защитой от гео-блокировок (bytick.com)
exchange = ccxt.bybit({
    'apiKey': BYBIT_DEMO_KEY,
    'secret': BYBIT_DEMO_SECRET,
    'enableRateLimit': True,
    'urls': {
        'api': {
            'spot': 'https://api.bytick.com',
            'contract': 'https://api.bytick.com',
            'unified': 'https://api.bytick.com',
        }
    },
    'options': {
        'defaultType': 'future',
        'defaultMarginMode': 'cross',  # Режим Кросс-маржи
    }
})
exchange.set_sandbox_mode(True)

# Переключение авторизации с автопереподключением при сетевых сбоях
if TELEGRAM_STRING_SESSION:
    client = TelegramClient(
        StringSession(TELEGRAM_STRING_SESSION), 
        TELEGRAM_API_ID, 
        TELEGRAM_API_HASH,
        auto_reconnect=True,
        connection_retries=None  # Бесконечные попытки переподключения в течение 6 часов
    )
else:
    client = TelegramClient('user_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)

def parse_asset(text: str):
    """
    Извлекает наименование актива из сигналов формата: 
    #ETH, #BTC, #TRUMP/USDT, BTCUSDT и служебных сообщений о ликвидациях.
    """
    # 1. Поиск выражений со знаком # (например, #ETH, #BTC, #TRUMP)
    hashtag_match = re.search(r'#([A-Z0-9]{2,15})', text.upper())
    if hashtag_match:
        raw_symbol = hashtag_match.group(1)
        ignored_words = {'BUY', 'SELL', 'LONG', 'SHORT', 'TP', 'SL', 'USDT', 'SIGNAL'}
        if raw_symbol not in ignored_words:
            return f"{raw_symbol}/USDT:USDT"

    # 2. Поиск стандартных пар с суффиксом USDT (#TRUMP/USDT, ETHUSDT)
    pair_match = re.search(r'#?([A-Z0-9]{2,15})(?:/USDT|USDT)', text.upper())
    if pair_match:
        raw_symbol = pair_match.group(1)
        ignored_words = {'BUY', 'SELL', 'LONG', 'SHORT', 'TP', 'SL', 'USDT', 'SIGNAL'}
        if raw_symbol not in ignored_words:
            return f"{raw_symbol}/USDT:USDT"

    return None

async def setup_symbol_config(symbol: str):
    """
    Настраивает Кросс-маржу и выставляет максимальное плечо для пары.
    """
    try:
        markets = await exchange.load_markets()
        market = markets.get(symbol)
        
        # 1. Установка Кросс-маржи
        try:
            await exchange.set_margin_mode('cross', symbol, params={'tradeMode': 0})
            print(f"[MARGIN] Кросс-маржа установлена для {symbol}")
        except Exception as e:
            print(f"[MARGIN NOTE] Инфо по марже ({symbol}): {e}")

        # 2. Получение и установка максимального плеча
        max_leverage = market.get('limits', {}).get('leverage', {}).get('max', 50) if market else 50
        await exchange.set_leverage(int(max_leverage), symbol)
        print(f"[LEVERAGE] Установлено плечо: {max_leverage}x для {symbol}")
        
        return int(max_leverage)
    except Exception as e:
        print(f"[WARNING] Ошибка настройки {symbol}: {e}. Используем плечо 20x.")
        return 20

async def open_dual_positions(symbol: str, margin_usdt: float = 10.0):
    """
    Открывает одновременно Long и Short на $10 маржи с Кросс-маржей, 
    макс. плечом, TP (+150% ROI) и SL (-100% ROI).
    """
    try:
        leverage = await setup_symbol_config(symbol)
        
        ticker = await exchange.fetch_ticker(symbol)
        entry_price = ticker['last']
        
        # Позиционный объем (Маржа * Плечо)
        position_value = margin_usdt * leverage
        raw_quantity = position_value / entry_price
        
        # Округление количества контрактов согласно точным правилам Bybit
        quantity = float(exchange.amount_to_precision(symbol, raw_quantity))
        
        # Расчет процента изменения цены для TP/SL
        tp_percent = 1.50 / leverage
        sl_percent = 1.00 / leverage
        
        # Цены TP/SL для Long
        long_tp = entry_price * (1 + tp_percent)
        long_sl = entry_price * (1 - sl_percent)
        
        # Цены TP/SL для Short
        short_tp = entry_price * (1 - tp_percent)
        short_sl = entry_price * (1 + sl_percent)

        print(f"\n[EXECUTION] Вход по {symbol} | Цена: {entry_price} | Маржа: ${margin_usdt} | Плечо: {leverage}x | Объём: {quantity}")

        # 1. Открытие LONG (positionIdx: 1)
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
        print(f"[LONG OPENED] TP: {long_tp:.4f} (+150%) | SL: {long_sl:.4f} (-100%)")

        # 2. Открытие SHORT (positionIdx: 2)
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
        print(f"[SHORT OPENED] TP: {short_tp:.4f} (+150%) | SL: {short_sl:.4f} (-100%)")

    except Exception as e:
        print(f"[ERROR] Ошибка выполнения ордеров: {e}")

@client.on(events.NewMessage(chats=TARGET_CHANNEL if TARGET_CHANNEL else None))
async def handle_new_message(event):
    message_text = event.raw_text
    print(f"\n[NEW SIGNAL] Сообщение: {message_text}")
    
    symbol = parse_asset(message_text)
    if symbol:
        print(f"[PARSED ASSET] Найден актив: {symbol}")
        await open_dual_positions(symbol, margin_usdt=10.0)
    else:
        print("[SKIP] Актив не распознан.")

async def main():
    print("Запуск бота...")
    await client.start()
    print("Бот запущен и ожидает сигналы...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        asyncio.run(exchange.close())
