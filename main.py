#!/usr/bin/env python3
"""
WAIFU NAME LOOKUP BOT
A Telegram bot that identifies anime characters and scenes from images using:
- SauceNAO API for character name lookup (100/day limit)
- trace.moe API for anime scene recognition (rate limited)
- Redis for fast caching (0.01s)
- MongoDB for permanent storage (0.05s)
"""

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

import aiohttp
from PIL import Image
import imagehash
from pyrogram import Client, filters
from pyrogram.types import Message
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import redis.asyncio as redis
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SAUCENAO_KEY = os.getenv("SAUCENAO_KEY", "")
TRACE_MOE_URL = "https://api.trace.moe/search"
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "[]").strip("[]").split(",") if x.strip()]

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "bot" / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


class DatabaseManager:
    def __init__(self):
        self.mongo_client = None
        self.redis_client = None
        self.db = None
        self.lookup_collection = None
        self.users_collection = None

    async def connect(self):
        self.mongo_client = MongoClient(MONGO_URL)
        self.db = self.mongo_client["waifu_bot"]
        self.lookup_collection = self.db["lookups"]
        self.users_collection = self.db["users"]
        self.lookup_collection.create_index([("image_hash", 1)], unique=True)
        self.users_collection.create_index([("user_id", 1)], unique=True)
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await self.redis_client.ping()

    async def close(self):
        if self.mongo_client:
            self.mongo_client.close()
        if self.redis_client:
            await self.redis_client.close()

    async def get_cached_result(self, image_hash):
        if not self.redis_client:
            return None
        cached = await self.redis_client.get(f"waifu:{image_hash}")
        return json.loads(cached) if cached else None

    async def set_cached_result(self, image_hash, result, ttl=86400):
        if not self.redis_client:
            return
        await self.redis_client.setex(f"waifu:{image_hash}", ttl, json.dumps(result))

    async def save_lookup(self, user_id, image_hash, result):
        if not self.lookup_collection:
            return False
        try:
            doc = {
                "user_id": user_id,
                "image_hash": image_hash,
                "result": result,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }
            self.lookup_collection.insert_one(doc)
            return True
        except DuplicateKeyError:
            self.lookup_collection.update_one(
                {"image_hash": image_hash},
                {"$set": {"result": result, "updated_at": datetime.utcnow()}}
            )
            return True
        except Exception as e:
            print(f"MongoDB error: {e}")
            return False

    async def update_user_stats(self, user_id):
        if not self.users_collection:
            return
        self.users_collection.update_one(
            {"user_id": user_id},
            {"$inc": {"total_lookups": 1}, "$set": {"last_lookup": datetime.utcnow()}},
            upsert=True
        )

    async def get_bot_stats(self):
        if not self.lookup_collection or not self.users_collection:
            return {}
        total_lookups = self.lookup_collection.count_documents({})
        total_users = self.users_collection.count_documents({})
        yesterday = datetime.utcnow() - timedelta(days=1)
        recent_lookups = self.lookup_collection.count_documents({"created_at": {"$gte": yesterday}})
        return {"total_lookups": total_lookups, "total_users": total_users, "recent_lookups": recent_lookups}

    async def clear_all_cache(self):
        if not self.redis_client:
            return False
        try:
            await self.redis_client.flushdb()
            return True
        except:
            return False


class SauceNAOClient:
    BASE_URL = "https://saucenao.com/search.php"
    def __init__(self, api_key):
        self.api_key = api_key
        self.session = aiohttp.ClientSession()

    async def search(self, image_url):
        if not self.api_key:
            return None
        params = {"api_key": self.api_key, "url": image_url, "output_type": 2, "numres": 5, "db": 999}
        try:
            async with self.session.get(self.BASE_URL, params=params, timeout=30) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._parse_results(data)
        except:
            pass
        return None

    def _parse_results(self, data):
        if not data or "results" not in data or not data["results"]:
            return None
        best = data["results"][0]
        header = best.get("header", {})
        data_info = best.get("data", {})
        result = {
            "source": "saucenao",
            "similarity": float(header.get("similarity", 0)),
            "thumbnail": header.get("thumbnail", ""),
            "index_name": header.get("index_name", "")
        }
        if "character" in data_info:
            result["character"] = data_info["character"]
        elif "name" in data_info:
            result["character"] = data_info["name"]
        if "source" in data_info:
            result["series"] = data_info["source"]
        if "url" in data_info:
            result["url"] = data_info["url"]
        elif "ext_urls" in data_info and data_info["ext_urls"]:
            result["url"] = data_info["ext_urls"][0]
        return result

    async def close(self):
        await self.session.close()


class TraceMoeClient:
    def __init__(self):
        self.session = aiohttp.ClientSession()

    async def search(self, image_path):
        try:
            with open(image_path, "rb") as f:
                files = {"image": f}
            async with self.session.post(TRACE_MOE_URL, files=files, timeout=30) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._parse_results(data)
        except:
            pass
        return None

    def _parse_results(self, data):
        if not data or "docs" not in data or not data["docs"]:
            return None
        best = data["docs"][0]
        return {
            "source": "trace.moe",
            "similarity": float(best.get("similarity", 0)),
            "anilist_id": int(best.get("anilist_id", 0)),
            "title_romaji": best.get("title_romaji", ""),
            "title_english": best.get("title_english", ""),
            "episode": int(best.get("episode", 0)),
            "from": float(best.get("from", 0)),
            "to": float(best.get("to", 0)),
            "anilist_url": best.get("anilist_url", ""),
            "mal_url": best.get("mal_url", ""),
            "is_adult": bool(best.get("is_adult", False))
        }

    async def close(self):
        await self.session.close()


class ImageProcessor:
    def __init__(self):
        self.downloads_dir = DOWNLOADS_DIR

    async def download_image(self, message):
        try:
            if message.photo:
                file_path = self.downloads_dir / f"{message.photo.file_id}.jpg"
                await message.download(file_path)
                return str(file_path)
            elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
                ext = message.document.mime_type.split("/")[-1]
                file_path = self.downloads_dir / f"{message.document.file_id}.{ext}"
                await message.download(file_path)
                return str(file_path)
        except Exception as e:
            print(f"Download error: {e}")
        return None

    def compute_image_hash(self, image_path):
        try:
            with Image.open(image_path) as img:
                phash = str(imagehash.phash(img))
                dhash = str(imagehash.dhash(img))
                ahash = str(imagehash.average_hash(img))
                combined = f"{phash}-{dhash}-{ahash}"
                return hashlib.sha256(combined.encode()).hexdigest()
        except Exception as e:
            print(f"Hash error: {e}")
        return None


class ResultFormatter:
    @staticmethod
    def format_saucenao(result):
        if not result:
            return "No character found"
        text = f"Character: {result.get('character', 'Unknown')}\n"
        text += f"Match: {result.get('similarity', 0):.2f}%\n"
        if result.get('series'):
            text += f"Series: {result['series']}\n"
        if result.get('url'):
            text += f"[More Info]({result['url']})\n"
        return text

    @staticmethod
    def format_trace_moe(result):
        if not result:
            return "No anime scene found"
        text = f"Anime: {result.get('title_romaji', 'Unknown')}\n"
        text += f"Match: {result.get('similarity', 0):.2f}%\n"
        if result.get('episode'):
            text += f"Episode: {result['episode']}\n"
        if result.get('from') and result.get('to'):
            m_f = int(result['from'] // 60)
            s_f = int(result['from'] % 60)
            m_t = int(result['to'] // 60)
            s_t = int(result['to'] % 60)
            text += f"Scene: {m_f}:{s_f:02d} - {m_t}:{s_t:02d}\n"
        if result.get('anilist_url'):
            text += f"[AniList]({result['anilist_url']})\n"
        if result.get('mal_url'):
            text += f"[MyAnimeList]({result['mal_url']})\n"
        if result.get('is_adult'):
            text += "NSFW\n"
        return text

    @staticmethod
    def format_combined(saucenao, trace_moe):
        parts = []
        if saucenao:
            parts.append("SauceNAO Results:\n")
            parts.append(ResultFormatter.format_saucenao(saucenao))
        if trace_moe:
            parts.append("\ntrace.moe Results:\n")
            parts.append(ResultFormatter.format_trace_moe(trace_moe))
        if not parts:
            return "No results found. Try a clearer image."
        return "".join(parts)

    @staticmethod
    def format_stats(stats):
        return (f"Bot Statistics\n\n"
                f"Total Lookups: {stats.get('total_lookups', 0):,}\n"
                f"Total Users: {stats.get('total_users', 0):,}\n"
                f"Lookups (24h): {stats.get('recent_lookups', 0):,}\n")


class BotHandlers:
    def __init__(self, app, db_manager):
        self.app = app
        self.db = db_manager
        self.saucenao = SauceNAOClient(SAUCENAO_KEY)
        self.trace_moe = TraceMoeClient()
        self.image_processor = ImageProcessor()

    async def handle_image(self, message):
        user_id = message.from_user.id
        await message.reply_chat_action("typing")
        image_path = await self.image_processor.download_image(message)
        if not image_path:
            await message.reply("Please send an image (JPG, PNG, WEBP)")
            return
        image_hash = self.image_processor.compute_image_hash(image_path)
        if not image_hash:
            await message.reply("Failed to process image")
            return
        cached_result = await self.db.get_cached_result(image_hash)
        if cached_result:
            await message.reply(f"Cached Result:\n\n{cached_result['formatted_text']}", reply_to_message_id=message.id)
            await self.db.update_user_stats(user_id)
            try:
                os.remove(image_path)
            except:
                pass
            return
        saucenao_task = asyncio.create_task(self._lookup_saucenao(image_path))
        trace_moe_task = asyncio.create_task(self._lookup_trace_moe(image_path))
        saucenao_result, trace_moe_result = await asyncio.gather(saucenao_task, trace_moe_task, return_exceptions=True)
        formatted_text = ResultFormatter.format_combined(
            saucenao_result if not isinstance(saucenao_result, Exception) else None,
            trace_moe_result if not isinstance(trace_moe_result, Exception) else None
        )
        result_data = {
            "formatted_text": formatted_text,
            "saucenao": saucenao_result if not isinstance(saucenao_result, Exception) else None,
            "trace_moe": trace_moe_result if not isinstance(trace_moe_result, Exception) else None,
            "timestamp": datetime.utcnow().isoformat()
        }
        await self.db.set_cached_result(image_hash, result_data)
        await self.db.save_lookup(user_id, image_hash, result_data)
        await self.db.update_user_stats(user_id)
        await message.reply(formatted_text, reply_to_message_id=message.id)
        try:
            os.remove(image_path)
        except:
            pass

    async def _lookup_saucenao(self, image_path):
        try:
            return await self.saucenao.search(f"file://{image_path}")
        except:
            return None

    async def _lookup_trace_moe(self, image_path):
        try:
            return await self.trace_moe.search(image_path)
        except:
            return None

    async def handle_waifu_command(self, message):
        help_text = (f"Waifu Name Lookup Bot\n\n"
                    f"Send me an anime image and I'll identify it!\n\n"
                    f"Commands:\n"
                    f"/waifu - Help\n"
                    f"/w - Help\n"
                    f"/name - Help\n"
                    f"/stats - Bot statistics\n")
        if message.from_user.id in ADMIN_IDS:
            help_text += "/clearcache - Clear cache (admin only)\n"
        await message.reply(help_text)

    async def handle_stats_command(self, message):
        stats = await self.db.get_bot_stats()
        await message.reply(ResultFormatter.format_stats(stats))

    async def handle_clearcache_command(self, message):
        if message.from_user.id not in ADMIN_IDS:
            await message.reply("You are not an admin")
            return
        success = await self.db.clear_all_cache()
        await message.reply("Cache cleared" if success else "Failed to clear cache")

    async def handle_start_command(self, message):
        await message.reply(f"Welcome to Waifu Name Lookup Bot!\n\nSend me an anime image and I'll identify the character or scene!\n\nUse /waifu for help.")

    async def close(self):
        await self.saucenao.close()
        await self.trace_moe.close()


async def main():
    print("Starting Waifu Name Lookup Bot...")
    db_manager = DatabaseManager()
    await db_manager.connect()
    app = Client("waifu_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    handlers = BotHandlers(app, db_manager)

    @app.on_message(filters.command(["start"]))
    async def start_handler(client, message):
        await handlers.handle_start_command(message)

    @app.on_message(filters.command(["waifu", "w", "name"]))
    async def waifu_handler(client, message):
        await handlers.handle_waifu_command(message)

    @app.on_message(filters.command(["stats"]))
    async def stats_handler(client, message):
        await handlers.handle_stats_command(message)

    @app.on_message(filters.command(["clearcache"]))
    async def clearcache_handler(client, message):
        await handlers.handle_clearcache_command(message)

    @app.on_message(filters.photo | (filters.document & filters.mime_type(["image/jpeg", "image/png", "image/webp"])))
    async def image_handler(client, message):
        await handlers.handle_image(message)

    await app.start()
    print("Bot is running. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await handlers.close()
        await db_manager.close()
        await app.stop()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
