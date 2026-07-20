import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    BOT_TOKEN = '8824483780:AAH7CES3hG69Kf0q_wA6D0oe1-tE0Lxz7pI'
    BOT_NAME = os.getenv('BOT_NAME', 'AutoReplyBot')