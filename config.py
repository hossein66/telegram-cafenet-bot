import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    BOT_TOKEN = '8059221192:AAFQaTcuFVSX4rcRSka0O3NRqspchlA4tts'
    BOT_NAME = os.getenv('BOT_NAME', 'AutoReplyBot')