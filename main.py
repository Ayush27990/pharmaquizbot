import os
import json
import re
import time
import logging
import asyncio
import io
import base64
import random

import PyPDF2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from groq import Groq

# ======================
# LOGGING
# ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ======================
# CONFIG
# ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = 723919716
INTERVAL = 3600  # 1 hour

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID missing")

client = Groq(api_key=GROQ_API_KEY)
pending
