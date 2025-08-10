import asyncio
import logging
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from config import settings
from database import Database
import handlers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    await Database.init()

    app = Application.builder().token(settings.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler('start', handlers.start))
    app.add_handler(CommandHandler('gym_task', handlers.gym_task))
    app.add_handler(CommandHandler('profile', handlers.profile))
    app.add_handler(CommandHandler('sendphoto', handlers.send_photo))
    app.add_handler(MessageHandler(filters.Regex("^🏋️ Получить задание$"), handlers.gym_task))
    app.add_handler(MessageHandler(filters.Regex("^📊 Профиль$"), handlers.profile))
    app.add_handler(CommandHandler('delete_db', handlers.delete_db))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))
    # app.add_handler(CallbackQueryHandler(handlers.menu_callback))

    logger.info('Бот запускается...')
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info('Завершение...')
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await Database.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Остановлено пользователем')