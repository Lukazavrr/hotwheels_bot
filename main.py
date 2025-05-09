from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from database import Session, Product, Cart
import os
from dotenv import load_dotenv
import logging
import asyncio
from typing import Dict, List, Optional
from io import BytesIO
from PIL import Image
import aiohttp
import concurrent.futures
import time

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
YOUR_TELEGRAM_TAG = os.getenv("YOUR_TELEGRAM_TAG", "@your_username")

# Проверка обязательных переменных
if not TOKEN:
    raise ValueError("Не указан TOKEN в переменных окружения")
if not ADMIN_ID:
    raise ValueError("Не указан ADMIN_ID в переменных окружения")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Оптимизация: кэш для изображений и пул потоков
image_cache = {}
executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# Хранилище для данных пользователей
user_data: Dict[int, Dict] = {}

def get_main_keyboard():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🏎 Мейн модели"),
             types.KeyboardButton(text="🚗 Спец. серии")],
            [types.KeyboardButton(text="🏁 Премиум модели"),
             types.KeyboardButton(text="🔮 Замак модели")],
            [types.KeyboardButton(text="🚚 Тим транспорт"),
             types.KeyboardButton(text="🛒 Корзина")],
            [types.KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )

def get_product_keyboard(product_id: int, category: str):
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"back_to_list_{category}"),
             types.InlineKeyboardButton(text="➕ В корзину", callback_data=f"add_{product_id}")]
        ]
    )

class OrderStates(StatesGroup):
    waiting_phone = State()
    waiting_payment_info = State()

class AddProduct(StatesGroup):
    waiting_photo = State()
    waiting_name = State()
    waiting_price = State()
    waiting_description = State()
    waiting_category = State()

class DeleteProduct(StatesGroup):
    waiting_id = State()

async def delete_previous_messages(chat_id: int, message_ids: List[int]):
    for msg_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.error(f"Ошибка при удалении сообщения: {e}")

async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[Image.Image]:
    """Асинхронная загрузка изображения с кэшированием"""
    if url in image_cache:
        return image_cache[url]
    
    try:
        start_time = time.time()
        async with session.get(url) as response:
            if response.status == 200:
                image_data = await response.read()
                logger.info(f"Загружено изображение {url} за {time.time()-start_time:.2f} сек")
                
                # Оптимизация: обработка в отдельном потоке
                loop = asyncio.get_running_loop()
                image = await loop.run_in_executor(
                    executor,
                    lambda: Image.open(BytesIO(image_data))
                )
                image.thumbnail((400, 400))
                
                image_cache[url] = image
                return image
    except Exception as e:
        logger.error(f"Ошибка загрузки изображения {url}: {e}")
    return None

def create_collage_sync(images: List[Image.Image]) -> BytesIO:
    """Создание коллажа с оптимизацией"""
    try:
        start_time = time.time()
        num_images = len(images)
        cols = min(3, num_images)
        rows = (num_images + cols - 1) // cols
        
        # Используем размер первого изображения
        img_width, img_height = images[0].size
        
        # Создаем новое изображение для коллажа
        collage = Image.new('RGB', (cols * img_width, rows * img_height))
        
        # Вставляем изображения в коллаж
        for i, img in enumerate(images):
            row = i // cols
            col = i % cols
            collage.paste(img, (col * img_width, row * img_height))
        
        # Оптимизация: уменьшаем качество
        buffer = BytesIO()
        collage.save(buffer, format='JPEG', quality=85, optimize=True)
        buffer.seek(0)
        
        logger.info(f"Коллаж создан за {time.time()-start_time:.2f} сек")
        return buffer
    except Exception as e:
        logger.error(f"Ошибка создания коллажа: {e}")
        return None

async def create_combined_message(photo_urls: List[str], products: List[Product], category_name: str) -> Optional[tuple]:
    """Создает объединенное сообщение с коллажем и списком товаров"""
    async with aiohttp.ClientSession() as session:
        # Загружаем изображения
        tasks = [download_image(session, url) for url in photo_urls]
        images = await asyncio.gather(*tasks)
        images = [img for img in images if img is not None]
        
        if not images:
            return None
        
        # Формируем текст списка товаров
        products_text = f"📋 {category_name} - список моделей:\n\n"
        for idx, product in enumerate(products, 1):
            products_text += f"{idx}. {product.name} - {product.price} руб.\n"
        
        # Создаем коллаж в отдельном потоке
        loop = asyncio.get_running_loop()
        collage_buffer = await loop.run_in_executor(
            executor,
            lambda: create_collage_sync(images)
        )
        
        if not collage_buffer:
            return None
            
        return collage_buffer, products_text, len(products)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🚗 Добро пожаловать в Hot Wheels Kriak Shop!\n\n"
        "Здесь вы можете найти нужную вам модель Hot Wheels.\n"
        "Выберите категорию:",
        reply_markup=get_main_keyboard()
    )

@dp.message(lambda message: message.text in [
    "🏎 Мейн модели", "🚗 Спец. серии", "🏁 Премиум модели",
    "🔮 Замак модели", "🚚 Тим транспорт"
])
async def show_category(message: types.Message):
    category_map = {
        "🏎 Мейн модели": "main",
        "🚗 Спец. серии": "special",
        "🏁 Премиум модели": "premium",
        "🔮 Замак модели": "zamak",
        "🚚 Тим транспорт": "team_transport"
    }
    
    user_id = message.from_user.id
    category = category_map[message.text]
    category_name = message.text
    
    session = Session()
    try:
        products = session.query(Product).filter(Product.category == category).all()
        
        if not products:
            await message.answer("В этой категории пока нет товаров 😢")
            return
        
        # Сохраняем список товаров для пользователя
        user_data[user_id] = {
            'category': category,
            'products': {p.id: p for p in products},
            'last_msg_ids': []
        }
        
        # Получаем URL фотографий
        file_tasks = [bot.get_file(product.photo_id) for product in products]
        files = await asyncio.gather(*file_tasks)
        photo_urls = [f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}" for file in files]
        
        # Создаем объединенное сообщение
        combined = await create_combined_message(photo_urls, products, category_name)
        
        if not combined:
            await message.answer("Ошибка при создании коллажа, показываем только список")
            await show_products_list(message, user_id)
            return
            
        collage_buffer, products_text, num_products = combined
        
        # Создаем кнопки для выбора модели
        buttons = []
        row = []
        for idx in range(1, num_products + 1):
            row.append(types.InlineKeyboardButton(
                text=str(idx),
                callback_data=f"product_{products[idx-1].id}"
            ))
            if idx % 3 == 0:  # 3 кнопки в строке
                buttons.append(row)
                row = []
        
        if row:  # Добавляем оставшиеся кнопки
            buttons.append(row)
        
        buttons.append([types.InlineKeyboardButton(
            text="⬅️ Назад в меню",
            callback_data="back_to_menu"
        )])
        
        # Удаляем предыдущие сообщения
        if user_id in user_data and user_data[user_id]['last_msg_ids']:
            await delete_previous_messages(message.chat.id, user_data[user_id]['last_msg_ids'])
        
        # Отправляем объединенное сообщение
        msg = await message.answer_photo(
            photo=types.BufferedInputFile(collage_buffer.read(), filename="collage.jpg"),
            caption=products_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        
        # Сохраняем ID сообщения
        user_data[user_id]['last_msg_ids'] = [msg.message_id]
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer("Произошла ошибка 😢 Попробуйте позже.")
    finally:
        session.close()

async def show_products_list(message: types.Message, user_id: int):
    if user_id not in user_data:
        await message.answer("Ошибка отображения списка")
        return
    
    products = list(user_data[user_id]['products'].values())
    category = user_data[user_id]['category']
    category_name = {
        "main": "🏎 Мейн модели",
        "special": "🚗 Спец. серии",
        "premium": "🏁 Премиум модели",
        "zamak": "🔮 Замак модели",
        "team_transport": "🚚 Тим транспорт"
    }.get(category, category)
    
    # Формируем текст списка товаров
    products_text = f"📋 {category_name} - список моделей:\n\n"
    for idx, product in enumerate(products, 1):
        products_text += f"{idx}. {product.name} - {product.price} руб.\n"
    
    # Создаем кнопки для выбора модели
    buttons = []
    row = []
    for idx, product in enumerate(products, 1):
        row.append(types.InlineKeyboardButton(
            text=str(idx),
            callback_data=f"product_{product.id}"
        ))
        if idx % 3 == 0:  # 3 кнопки в строке
            buttons.append(row)
            row = []
    
    if row:  # Добавляем оставшиеся кнопки
        buttons.append(row)
    
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад в меню",
        callback_data="back_to_menu"
    )])
    
    try:
        # Удаляем предыдущие сообщения
        if user_data[user_id]['last_msg_ids']:
            await delete_previous_messages(message.chat.id, user_data[user_id]['last_msg_ids'])
        
        # Отправляем список
        msg = await message.answer(
            products_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        
        # Сохраняем ID сообщения
        user_data[user_id]['last_msg_ids'] = [msg.message_id]
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer("Произошла ошибка 😢 Попробуйте позже.")

@dp.callback_query(lambda c: c.data.startswith('product_'))
async def show_product(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    product_id = int(callback.data.split('_')[1])
    
    if user_id not in user_data or product_id not in user_data[user_id]['products']:
        await callback.answer("Товар не найден")
        return
    
    product = user_data[user_id]['products'][product_id]
    category = user_data[user_id]['category']
    
    try:
        # Удаляем предыдущие сообщения
        if user_data[user_id]['last_msg_ids']:
            await delete_previous_messages(callback.message.chat.id, user_data[user_id]['last_msg_ids'])
        
        # Отправляем фото товара
        msg = await callback.message.answer_photo(
            photo=product.photo_id,
            caption=f"<b>🚀 {product.name}</b>\n💵 Цена: {product.price} руб.\n📝 Описание: {product.description}",
            reply_markup=get_product_keyboard(product.id, category),
            parse_mode=ParseMode.HTML
        )
        
        # Сохраняем ID сообщения
        user_data[user_id]['last_msg_ids'] = [msg.message_id]
        
    except Exception as e:
        logger.error(f"Ошибка показа товара: {e}")
        await callback.message.answer("Ошибка отображения товара 😢 Попробуйте позже.")
    
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.message.answer(
        "Выберите категорию:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith('back_to_list_'))
async def back_to_list(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    category = callback.data.split('_')[-1]
    
    if user_id not in user_data or user_data[user_id]['category'] != category:
        await callback.answer("Ошибка навигации")
        return
    
    # Получаем название категории для отображения
    category_name = {
        "main": "🏎 Мейн модели",
        "special": "🚗 Спец. серии", 
        "premium": "🏁 Премиум модели",
        "zamak": "🔮 Замак модели",
        "team_transport": "🚚 Тим транспорт"
    }.get(category, category)
    
    # Удаляем предыдущие сообщения
    if user_id in user_data and user_data[user_id]['last_msg_ids']:
        await delete_previous_messages(callback.message.chat.id, user_data[user_id]['last_msg_ids'])
    
    session = Session()
    try:
        products = session.query(Product).filter(Product.category == category).all()
        
        if not products:
            await callback.message.answer("В этой категории пока нет товаров 😢")
            return
        
        # Обновляем список товаров для пользователя
        user_data[user_id]['products'] = {p.id: p for p in products}
        
        # Получаем URL фотографий
        file_tasks = [bot.get_file(product.photo_id) for product in products]
        files = await asyncio.gather(*file_tasks)
        photo_urls = [f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}" for file in files]
        
        # Создаем объединенное сообщение
        combined = await create_combined_message(photo_urls, products, category_name)
        
        if not combined:
            await callback.message.answer("Ошибка при создании коллажа, показываем только список")
            await show_products_list(callback.message, user_id)
            return
            
        collage_buffer, products_text, num_products = combined
        
        # Создаем кнопки для выбора модели
        buttons = []
        row = []
        for idx in range(1, num_products + 1):
            row.append(types.InlineKeyboardButton(
                text=str(idx),
                callback_data=f"product_{products[idx-1].id}"
            ))
            if idx % 3 == 0:  # 3 кнопки в строке
                buttons.append(row)
                row = []
        
        if row:  # Добавляем оставшиеся кнопки
            buttons.append(row)
        
        buttons.append([types.InlineKeyboardButton(
            text="⬅️ Назад в меню",
            callback_data="back_to_menu"
        )])
        
        # Отправляем объединенное сообщение
        msg = await callback.message.answer_photo(
            photo=types.BufferedInputFile(collage_buffer.read(), filename="collage.jpg"),
            caption=products_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        
        # Сохраняем ID сообщения
        user_data[user_id]['last_msg_ids'] = [msg.message_id]
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await callback.message.answer("Произошла ошибка 😢 Попробуйте позже.")
    finally:
        session.close()
    
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith('add_'))
async def add_to_cart(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    product_id = int(callback.data.split('_')[1])
    
    session = Session()
    try:
        exists = session.query(Cart).filter(
            Cart.user_id == user_id,
            Cart.product_id == product_id
        ).first()
        
        if not exists:
            cart_item = Cart(user_id=user_id, product_id=product_id)
            session.add(cart_item)
            session.commit()
            await callback.answer("Товар добавлен в корзину! 🛒")
        else:
            await callback.answer("Этот товар уже в корзине")
    except Exception as e:
        logger.error(f"Ошибка добавления: {e}")
        await callback.answer("Ошибка при добавлении 😢")
    finally:
        session.close()

@dp.message(lambda message: message.text == "🛒 Корзина")
async def handle_cart_message(message: types.Message):
    await show_cart(message)

async def show_cart(message: types.Message):
    user_id = message.from_user.id
    session = Session()
    
    try:
        cart_items = session.query(Cart).filter(Cart.user_id == user_id).all()
        
        if not cart_items:
            await message.answer("Ваша корзина пуста 🛒", reply_markup=get_main_keyboard())
            return
            
        total = 0
        cart_text = "<b>🛒 Ваша корзина:</b>\n\n"
        
        for item in cart_items:
            product = session.query(Product).filter(Product.id == item.product_id).first()
            cart_text += f"• {product.name} - {product.price} руб. [<a href='tg://btn/{item.id}'>❌</a>]\n"
            total += product.price
        
        cart_text += f"\n💸 Итого к оплате: <b>{total} руб.</b>"
        
        msg = await message.answer(
            cart_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(text="❌ Очистить корзину", callback_data="clear_cart"),
                        types.InlineKeyboardButton(text="💳 Оформить заказ", callback_data="checkout")
                    ]
                ]
            ),
            parse_mode=ParseMode.HTML
        )
        
        # Сохраняем ID сообщения
        if user_id in user_data:
            user_data[user_id]['last_msg_ids'] = [msg.message_id]
            
    except Exception as e:
        await message.answer("Произошла ошибка при загрузке корзины 😢")
        logger.error(f"Ошибка загрузки корзины: {e}")
    finally:
        session.close()

@dp.callback_query(lambda c: c.data.startswith('remove_'))
async def remove_from_cart(callback: types.CallbackQuery):
    cart_id = int(callback.data.split('_')[1])
    user_id = callback.from_user.id
    
    session = Session()
    try:
        cart_item = session.query(Cart).filter(
            Cart.id == cart_id,
            Cart.user_id == user_id
        ).first()
        
        if cart_item:
            product_name = cart_item.product.name
            session.delete(cart_item)
            session.commit()
            await callback.answer(f"Товар {product_name} удален из корзины")
            await show_cart(callback.message)
        else:
            await callback.answer("Товар не найден в корзине")
    except Exception as e:
        await callback.answer("Ошибка при удалении 😢")
        logger.error(f"Ошибка удаления из корзины: {e}")
    finally:
        session.close()

@dp.callback_query(lambda c: c.data == "clear_cart")
async def clear_cart(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    session = Session()
    try:
        session.query(Cart).filter(Cart.user_id == user_id).delete()
        session.commit()
        await callback.answer("Корзина очищена!")
        await callback.message.answer("Ваша корзина пуста 🛒", reply_markup=get_main_keyboard())
    except Exception as e:
        await callback.answer("Ошибка при очистке корзины 😢")
        logger.error(f"Ошибка очистки корзины: {e}")
    finally:
        session.close()

@dp.callback_query(lambda c: c.data == "checkout")
async def start_checkout(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "Для оформления заказа нам нужны ваши контактные данные.\n"
        "Пожалуйста, отправьте ваш тег в телеграмм (@username) или номер телефона:",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="Отправить контакт", request_contact=True)],
                [types.KeyboardButton(text="Отменить заказ")]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    await state.set_state(OrderStates.waiting_phone)

@dp.message(OrderStates.waiting_phone, F.contact | F.text)
async def process_phone(message: types.Message, state: FSMContext):
    if message.text == "Отменить заказ":
        await message.answer("Оформление заказа отменено", reply_markup=get_main_keyboard())
        await state.clear()
        return
        
    contact = message.contact.phone_number if message.contact else message.text
    await state.update_data(contact=contact)
    await message.answer(
        "Теперь укажите предпочитаемый способ оплаты (карта, наличные и т.д.):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(OrderStates.waiting_payment_info)

@dp.message(OrderStates.waiting_payment_info)
async def process_payment_info(message: types.Message, state: FSMContext):
    payment_info = message.text
    data = await state.get_data()
    contact = data.get('contact', 'не указан')
    user_id = message.from_user.id
    user_tag = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"
    
    session = Session()
    try:
        cart_items = session.query(Cart).filter(Cart.user_id == user_id).all()
        
        if not cart_items:
            await message.answer("Ваша корзина пуста!", reply_markup=get_main_keyboard())
            await state.clear()
            return
            
        # Формируем сообщение для пользователя
        order_text = "✅ Ваш заказ оформлен!\n\n"
        order_text += f"📞 Ваши контакты: {contact}\n"
        order_text += f"💳 Способ оплаты: {payment_info}\n\n"
        order_text += "🛒 Состав заказа:\n"
        
        # Формируем сообщение для администратора
        admin_order_text = "🆕 НОВЫЙ ЗАКАЗ!\n\n"
        admin_order_text += f"👤 Пользователь: {user_tag}\n"
        admin_order_text += f"📞 Контакты: {contact}\n"
        admin_order_text += f"💳 Способ оплаты: {payment_info}\n\n"
        admin_order_text += "📦 Заказанные товары:\n"
        
        total = 0
        for item in cart_items:
            product = session.query(Product).filter(Product.id == item.product_id).first()
            order_text += f"• {product.name} - {product.price} руб.\n"
            admin_order_text += f"• {product.name} - {product.price} руб.\n"
            total += product.price
        
        order_text += f"\n💸 Итого к оплате: <b>{total} руб.</b>\n\n"
        order_text += "Спасибо за заказ! Мы свяжемся с вами в ближайшее время."
        
        admin_order_text += f"\n💰 Общая сумма: <b>{total} руб.</b>\n"
        admin_order_text += "\n✉️ Пользователь ожидает вашего ответа!"
        
        # Очищаем корзину
        session.query(Cart).filter(Cart.user_id == user_id).delete()
        session.commit()
        
        # Отправляем подтверждение пользователю
        await message.answer(order_text, reply_markup=get_main_keyboard())
        
        # Уведомляем администратора
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_order_text,
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        await message.answer("Произошла ошибка при оформлении заказа 😢")
        logger.error(f"Ошибка оформления заказа: {e}")
    finally:
        session.close()
        await state.clear()

@dp.message(lambda message: message.text == "❓ Помощь")
async def show_help(message: types.Message):
    help_text = (
        "ℹ️ <b>Помощь и поддержка</b>\n\n"
        "Если у вас возникли вопросы или проблемы с заказом, вы можете связаться с нами:\n\n"
        f"👨‍💻 Тег для связи: <code>{YOUR_TELEGRAM_TAG}</code>\n\n"
        "Просто нажмите на тег выше, чтобы скопировать его и связаться с нами в Telegram.\n\n"
        "Мы постараемся ответить вам как можно скорее!"
    )
    
    await message.answer(
        help_text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору")
        return
        
    await message.answer("Отправьте фото товара")
    await state.set_state(AddProduct.waiting_photo)

@dp.message(AddProduct.waiting_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await message.answer("Отлично! Теперь введите название товара:")
    await state.set_state(AddProduct.waiting_name)

@dp.message(AddProduct.waiting_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Введите цену товара (только число):")
    await state.set_state(AddProduct.waiting_price)

@dp.message(AddProduct.waiting_price)
async def process_price(message: types.Message, state: FSMContext):
    try:
        price = int(message.text)
        await state.update_data(price=price)
        await message.answer("Введите описание товара:")
        await state.set_state(AddProduct.waiting_description)
    except ValueError:
        await message.answer("❌ Цена должна быть числом! Попробуйте еще раз")

@dp.message(AddProduct.waiting_description)
async def process_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Мейн модели (main)")],
            [types.KeyboardButton(text="Спец. серии (special)")],
            [types.KeyboardButton(text="Премиум модели (premium)")],
            [types.KeyboardButton(text="Замак модели (zamak)")],
            [types.KeyboardButton(text="Тим транспорт (team_transport)")]
        ],
        resize_keyboard=True
    )
    
    await message.answer("Выберите категорию:", reply_markup=keyboard)
    await state.set_state(AddProduct.waiting_category)

@dp.message(AddProduct.waiting_category)
async def process_category(message: types.Message, state: FSMContext):
    category_map = {
        "Мейн модели (main)": "main",
        "Спец. серии (special)": "special",
        "Премиум модели (premium)": "premium",
        "Замак модели (zamak)": "zamak",
        "Тим транспорт (team_transport)": "team_transport"
    }
    
    if message.text not in category_map:
        await message.answer("❌ Выберите категорию из предложенных")
        return
    
    data = await state.get_data()
    category = category_map[message.text]
    
    session = Session()
    try:
        new_product = Product(
            category=category,
            name=data['name'],
            price=data['price'],
            photo_id=data['photo_id'],
            description=data['description']
        )
        
        session.add(new_product)
        session.commit()
        await message.answer(
            f"✅ Товар успешно добавлен в категорию {category}!\n\n"
            f"Название: {new_product.name}\n"
            f"Цена: {new_product.price} руб.",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка добавления товара: {e}")
        await message.answer("❌ Ошибка при сохранении товара")
    finally:
        session.close()
        await state.clear()

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору")
        return
        
    session = Session()
    try:
        products = session.query(Product).all()
        if not products:
            await message.answer("Нет товаров для удаления")
            return
            
        products_list = "\n".join([f"{p.id}: {p.name}" for p in products])
        await message.answer(
            f"Список товаров (ID: Название):\n{products_list}\n\n"
            "Введите ID товаров для удаления через пробел:"
        )
        await state.set_state(DeleteProduct.waiting_id)
    finally:
        session.close()

@dp.message(DeleteProduct.waiting_id)
async def process_delete_id(message: types.Message, state: FSMContext):
    try:
        # Получаем все ID из сообщения
        ids = message.text.split()
        deleted_products = []
        not_found_ids = []
        
        session = Session()
        
        for id_str in ids:
            try:
                product_id = int(id_str)
                product = session.query(Product).filter(Product.id == product_id).first()
                
                if product:
                    session.delete(product)
                    deleted_products.append(product.name)
                else:
                    not_found_ids.append(str(product_id))
            except ValueError:
                not_found_ids.append(id_str)
        
        session.commit()
        
        response = ""
        if deleted_products:
            response += "✅ Удалены товары:\n" + "\n".join(f"• {name}" for name in deleted_products) + "\n\n"
        if not_found_ids:
            response += "❌ Не найдены товары с ID: " + ", ".join(not_found_ids)
            
        await message.answer(response if response else "Ничего не удалено", reply_markup=get_main_keyboard())
        
    except Exception as e:
        await message.answer(f"❌ Произошла ошибка при удалении: {str(e)}")
        logger.error(f"Ошибка удаления товаров: {e}")
    finally:
        session.close()
        await state.clear()

@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"Ваш ID: {message.from_user.id}\n"
                       f"Ваш тег: @{message.from_user.username if message.from_user.username else 'отсутствует'}")

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    print("Бот запущен! 🚀")
    asyncio.run(main())