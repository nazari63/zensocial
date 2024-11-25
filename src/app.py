import os
import asyncio
from datetime import datetime, timezone
from typing import Optional, Tuple, Callable, Any, Dict
from io import BytesIO
import logging
from logging.handlers import RotatingFileHandler
import base64
import re
import random
import json

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
import tweepy
import requests
from bs4 import BeautifulSoup

# Create logs directory if it doesn't exist
os.makedirs('logs', exist_ok=True)

# Configure logging with more detailed format
logging.basicConfig(
    format='%(asctime)s - %(name)s - [%(levelname)s] - %(message)s',
    level=logging.INFO,
    handlers=[
        RotatingFileHandler(
            'logs/social_bot.log',
            maxBytes=1024*1024,  # 1MB
            backupCount=5
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

def get_env_var(var_name: str) -> str:
    """Get environment variable with error checking"""
    value = os.getenv(var_name)
    if not value:
        raise ValueError(f"Missing required environment variable: {var_name}")
    logger.debug(f"Loaded environment variable: {var_name}")
    return value

try:
    TELEGRAM_TOKEN = get_env_var("TELEGRAM_BOT_TOKEN")
    TWITTER_CONSUMER_KEY = get_env_var("TWITTER_CONSUMER_KEY")
    TWITTER_CONSUMER_SECRET = get_env_var("TWITTER_CONSUMER_SECRET")
    TWITTER_ACCESS_TOKEN = get_env_var("TWITTER_ACCESS_TOKEN")
    TWITTER_ACCESS_TOKEN_SECRET = get_env_var("TWITTER_ACCESS_TOKEN_SECRET")
    FARCASTER_AUTH_HEADER = get_env_var("FARCASTER_AUTHORIZATION_HEADER")
    IMGUR_CLIENT_ID = get_env_var("IMGUR_CLIENT_ID")
    BLUESKY_HANDLE = get_env_var("BLUESKY_HANDLE")
    BLUESKY_APP_PASSWORD = get_env_var("BLUESKY_APP_PASSWORD")
except ValueError as e:
    logger.error(str(e))
    raise

# Initialize Twitter clients
try:
    # Twitter v2 client for tweets
    twitter_client = tweepy.Client(
        consumer_key=TWITTER_CONSUMER_KEY,
        consumer_secret=TWITTER_CONSUMER_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
    )
    
    # Twitter v1.1 API for media upload
    auth = tweepy.OAuth1UserHandler(
        TWITTER_CONSUMER_KEY,
        TWITTER_CONSUMER_SECRET,
        TWITTER_ACCESS_TOKEN,
        TWITTER_ACCESS_TOKEN_SECRET
    )
    twitter_api = tweepy.API(auth)
    logger.info("Twitter clients initialized successfully")
    
except Exception as e:
    logger.error(f"Error initializing Twitter clients: {str(e)}")
    raise

# Store pending posts with their tasks
pending_posts = {}

class BlueskyClient:
    def __init__(self, handle: str, password: str):
        self.handle = handle
        self.password = password
        self.session = None
        
    async def create_session(self):
        """Create or refresh Bluesky session"""
        try:
            resp = requests.post(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                json={"identifier": self.handle, "password": self.password},
            )
            if resp.status_code == 401:
                logger.error("Bluesky authentication failed. Please ensure you're using an App Password from Settings → App passwords")
                raise Exception("Invalid Bluesky credentials - make sure to use an App Password")
            resp.raise_for_status()
            self.session = resp.json()
            logger.info("Bluesky session created successfully")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error creating Bluesky session: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in create_session: {str(e)}")
            raise

    async def upload_image(self, image_data: BytesIO) -> Dict:
        """Upload image to Bluesky and return blob object"""
        try:
            if not self.session:
                await self.create_session()
                
            image_data.seek(0)
            img_bytes = image_data.read()
            
            # Check size limit (1MB)
            if len(img_bytes) > 1000000:
                raise Exception("Image file size too large. 1MB maximum.")
                
            resp = requests.post(
                "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
                headers={
                    "Content-Type": "image/jpeg",
                    "Authorization": f"Bearer {self.session['accessJwt']}",
                },
                data=img_bytes,
            )
            resp.raise_for_status()
            return resp.json()["blob"]
        except Exception as e:
            logger.error(f"Error uploading image to Bluesky: {str(e)}")
            raise

    def format_text(self, text: str) -> str:
        """Format text with proper line break handling"""
        # Normalize line endings
        formatted_text = text.replace('\r\n', '\n').replace('\r', '\n')
        # Collapse multiple consecutive line breaks into two
        formatted_text = re.sub(r'\n{3,}', '\n\n', formatted_text)
        # Remove trailing whitespace from each line while preserving line breaks
        formatted_text = '\n'.join(line.rstrip() for line in formatted_text.splitlines())
        return formatted_text.strip()

    def parse_links(self, text: str):
        """Parse URLs and create facets for links"""
        facets = []
        text_bytes = text.encode('UTF-8')
        
        # Find all URLs in the text
        pattern = rb'https?://\S+'
        for match in re.finditer(pattern, text_bytes):
            start, end = match.span()
            url = match.group().decode('UTF-8')
            
            facets.append({
                "index": {
                    "byteStart": start,
                    "byteEnd": end
                },
                "features": [{
                    "$type": "app.bsky.richtext.facet#link",
                    "uri": url
                }]
            })
        
        return facets

    async def create_post(self, text: str, image_data: Optional[BytesIO] = None) -> str:
        """Create a Bluesky post with proper text formatting, links, and optional image"""
        try:
            if not self.session:
                await self.create_session()

            # Format text with proper line break handling
            formatted_text = self.format_text(text)

            # Create base post record
            post = {
                "$type": "app.bsky.feed.post",
                "text": formatted_text,
                "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }

            # Add facets for links
            facets = self.parse_links(formatted_text)
            if facets:
                post["facets"] = facets
                logger.info(f"Added {len(facets)} link facets to post")

            # Add image if provided
            if image_data:
                blob = await self.upload_image(image_data)
                post["embed"] = {
                    "$type": "app.bsky.embed.images",
                    "images": [{
                        "alt": "Attached image",
                        "image": blob,
                    }],
                }

            # Create the post
            resp = requests.post(
                "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                headers={"Authorization": f"Bearer {self.session['accessJwt']}"},
                json={
                    "repo": self.session["did"],
                    "collection": "app.bsky.feed.post",
                    "record": post,
                },
            )
            resp.raise_for_status()
            return resp.json()["uri"]

        except Exception as e:
            logger.error(f"Error creating Bluesky post: {str(e)}")
            raise

# Initialize Bluesky client
bluesky_client = BlueskyClient(BLUESKY_HANDLE, BLUESKY_APP_PASSWORD)

def clean_temp_file(file_path: str) -> None:
    """Safely remove a temporary file"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.debug(f"Cleaned up temporary file: {file_path}")
    except Exception as e:
        logger.error(f"Error cleaning up temp file {file_path}: {str(e)}")

def exponential_backoff(attempt: int, max_delay: int = 32) -> float:
    """Calculate delay with exponential backoff and jitter"""
    delay = min(max_delay, (2 ** attempt))
    jitter = random.uniform(0, 0.1 * delay)
    final_delay = delay + jitter
    logger.info(f"Calculated backoff delay: {final_delay:.2f}s (attempt {attempt + 1})")
    return final_delay

async def post_with_retry(func: Callable, *args, max_retries: int = 3, **kwargs) -> Any:
    """Generic retry handler with exponential backoff"""
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Attempting request (attempt {attempt + 1}/{max_retries})")
            return await func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            
            # Check if it's a rate limit error
            if hasattr(e, 'response') and e.response.status_code == 429:
                delay = exponential_backoff(attempt)
                logger.warning(f"Rate limited, waiting {delay:.2f}s before retry {attempt + 1}")
                await asyncio.sleep(delay)
                continue
            
            # If it's not a rate limit error, raise immediately
            logger.error(f"Non-rate-limit error encountered: {str(e)}")
            raise
    
    # If we've exhausted retries, log and raise the last exception
    logger.error(f"Exhausted all {max_retries} retries")
    raise last_exception

def extract_and_clean_text(text: str, for_farcaster: bool = False) -> Tuple[str, list[str]]:
    """Extract URLs from text and clean them from the content"""
    url_pattern = r'https?://\S+|www\.\S+'
    urls = re.findall(url_pattern, text)
    
    # Filter URLs that are <= 256 bytes
    valid_urls = [url for url in urls if len(url.encode('utf-8')) <= 256][:2]
    if len(valid_urls) < len(urls):
        logger.warning(f"Some URLs were filtered due to length or limit. Valid: {len(valid_urls)}, Total: {len(urls)}")
    
    # Clean URLs from text
    cleaned_text = text
    for url in urls:
        cleaned_text = cleaned_text.replace(url, '').strip()
    
    if for_farcaster:
        cleaned_text = cleaned_text.replace('\r\n', '\n').replace('\r', '\n')
        cleaned_text = re.sub(r'\n\s*\n', '\n\n', cleaned_text)
        cleaned_text = '\n'.join(line.rstrip() for line in cleaned_text.splitlines())
        logger.debug("Processed text for Farcaster with line break preservation")
    else:
        cleaned_text = re.sub(r'\s+', ' ', cleaned_text)
    
    cleaned_text = cleaned_text.strip()
    logger.debug(f"Extracted {len(valid_urls)} valid URLs from text")
    return cleaned_text, valid_urls

def upload_to_imgur(image_data: BytesIO) -> str:
    """Upload image to Imgur and return the URL"""
    try:
        image_data.seek(0)
        headers = {
            'Authorization': f'Client-ID {IMGUR_CLIENT_ID}',
        }
        
        base64_image = base64.b64encode(image_data.read())
        logger.info("Uploading image to Imgur")
        
        response = requests.post(
            'https://api.imgur.com/3/image',
            headers=headers,
            data={
                'image': base64_image,
                'type': 'base64'
            }
        )
        
        response.raise_for_status()
        
        if 'data' not in response.json() or 'link' not in response.json()['data']:
            raise ValueError("Invalid response from Imgur API")
            
        imgur_url = response.json()['data']['link']
        logger.info(f"Successfully uploaded image to Imgur: {imgur_url}")
        return imgur_url
        
    except Exception as e:
        logger.error(f"Imgur upload error: {str(e)}")
        raise
        
async def post_to_social(text: str, image_data: Optional[BytesIO] = None) -> tuple[Optional[str], Optional[str], Optional[int], str]:
    """Post content to Twitter, Farcaster, and Bluesky independently"""
    tweet_url = None
    bluesky_uri = None
    farcaster_status = None
    temp_file = None
    twitter_success = False
    farcaster_success = False
    bluesky_success = False
    
    try:
        # Extract URLs and clean text differently for each platform
        logger.info("Processing text for different platforms")
        twitter_text = text  # Keep original text for Twitter
        farcaster_text, urls = extract_and_clean_text(text, for_farcaster=True)
        bluesky_text = text  # Keep original text for Bluesky
        
        # Post to Twitter with retry
        try:
            async def post_to_twitter():
                nonlocal temp_file
                logger.info("Preparing Twitter post")
                if image_data:
                    image_data.seek(0)
                    temp_file = f'temp_image_{datetime.now().timestamp()}.jpg'
                    with open(temp_file, 'wb') as f:
                        f.write(image_data.read())
                    
                    media = twitter_api.media_upload(filename=temp_file)
                    return twitter_client.create_tweet(text=twitter_text, media_ids=[media.media_id])
                else:
                    return twitter_client.create_tweet(text=twitter_text)
            
            tweet = await post_with_retry(post_to_twitter)
            tweet_url = f"https://twitter.com/user/status/{tweet.data['id']}"
            logger.info(f"Successfully posted to Twitter: {tweet_url}")
            twitter_success = True
        except Exception as e:
            logger.error(f"Twitter post failed: {str(e)}")
        
        # Post to Farcaster with retry
        try:
            async def post_to_farcaster():
                nonlocal image_data
                logger.info("Preparing Farcaster post")
                farcaster_payload = {'text': farcaster_text}
                embeds = []
                
                if image_data:
                    image_data.seek(0)
                    imgur_url = upload_to_imgur(image_data)
                    embeds.append(imgur_url)
                    farcaster_payload['text'] += '\n'
                
                remaining_slots = 2 - len(embeds)
                if remaining_slots > 0 and urls:
                    embeds.extend(urls[:remaining_slots])
                
                if embeds:
                    farcaster_payload['embeds'] = embeds
                    logger.info(f"Farcaster embeds prepared: {embeds}")
                
                response = requests.post(
                    'https://api.warpcast.com/v2/casts',
                    headers={
                        'Authorization': FARCASTER_AUTH_HEADER,
                        'Content-Type': 'application/json'
                    },
                    json=farcaster_payload
                )
                response.raise_for_status()
                return response.status_code
            
            farcaster_status = await post_with_retry(post_to_farcaster)
            logger.info(f"Successfully posted to Farcaster with status: {farcaster_status}")
            farcaster_success = True
        except Exception as e:
            logger.error(f"Farcaster post failed: {str(e)}")

        # Post to Bluesky with retry
        try:
            async def post_to_bluesky():
                logger.info("Preparing Bluesky post")
                if image_data:
                    image_data.seek(0)
                return await bluesky_client.create_post(bluesky_text, image_data)

            bluesky_uri = await post_with_retry(post_to_bluesky)
            logger.info(f"Successfully posted to Bluesky: {bluesky_uri}")
            bluesky_success = True
        except Exception as e:
            logger.error(f"Bluesky post failed: {str(e)}")
        
        # Determine status message
        successes = []
        failures = []
        
        if bluesky_success:
            successes.append("Bluesky")
        else:
            failures.append("Bluesky")
            
        if farcaster_success:
            successes.append("Farcaster")
        else:
            failures.append("Farcaster")
            
        if twitter_success:
            successes.append("X")
        else:
            failures.append("X")
        
        # Sort alphabetically
        successes.sort()
        failures.sort()
        
        status_parts = []
        if successes:
            status_parts.append(f"Posted: {', '.join(successes)}")
        if failures:
            status_parts.append(f"Failed: {', '.join(failures)}")
        
        status_msg = " / " if failures else ""
        status_msg = status_msg.join(status_parts)
        
        return tweet_url, bluesky_uri, farcaster_status, status_msg
        
    finally:
        if temp_file:
            clean_temp_file(temp_file)

async def wait_and_post(user_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Wait for potential image and post content"""
    try:
        await asyncio.sleep(36)
        if user_id in pending_posts and not pending_posts[user_id].get('image_data'):
            post_data = pending_posts[user_id]
            logger.info(f"Processing text-only post for user {user_id}")
            tweet_url, bluesky_uri, farcaster_status, status_msg = await post_to_social(post_data['text'])
            
            chat_id = post_data.get('chat_id')
            if chat_id:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=status_msg
                )
            
    except Exception as e:
        logger.error(f"Error in wait_and_post: {str(e)}", exc_info=True)
        chat_id = pending_posts[user_id].get('chat_id') if user_id in pending_posts else None
        if chat_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Failed posting to social platforms"
            )
    finally:
        if user_id in pending_posts:
            del pending_posts[user_id]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages"""
    user_id = str(update.effective_user.id)
    text = update.message.text
    
    # Extract URLs and log them
    _, urls = extract_and_clean_text(text)
    if urls:
        logger.info(f"Found URLs in message from user {user_id}: {urls}")
    
    # Cancel existing wait task if any
    if user_id in pending_posts and pending_posts[user_id].get('task'):
        logger.info(f"Cancelling existing wait task for user {user_id}")
        pending_posts[user_id]['task'].cancel()
    
    # Create new wait task
    task = asyncio.create_task(wait_and_post(user_id, context))
    
    # Store text, task, and chat_id
    pending_posts[user_id] = {
        'text': text,
        'timestamp': datetime.now(),
        'task': task,
        'chat_id': update.effective_chat.id
    }
    
    logger.info(f"Received text from user {user_id}, waiting for potential image")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos"""
    user_id = str(update.effective_user.id)
    
    if user_id not in pending_posts:
        await update.message.reply_text("Please send text first.")
        return
    
    # Store the text before any cleanup
    text = pending_posts[user_id]['text']
    logger.info(f"Processing photo from user {user_id}")
    
    # Cancel the wait task
    if pending_posts[user_id].get('task'):
        pending_posts[user_id]['task'].cancel()
    
    try:
        # Get the highest resolution photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        logger.info(f"Downloading photo from Telegram: {photo.file_id}")
        
        # Download photo directly to BytesIO
        image_data = BytesIO()
        await file.download_to_memory(image_data)
        
        # Post content with image
        tweet_url, bluesky_uri, farcaster_status, status_msg = await post_to_social(text, image_data)
        await update.message.reply_text(status_msg)
        
    except Exception as e:
        logger.error(f"Error handling photo: {str(e)}", exc_info=True)
        await update.message.reply_text("Failed posting to social platforms")
    finally:
        if user_id in pending_posts:
            del pending_posts[user_id]

def main():
    """Main function to run the bot"""
    try:
        # Initialize bot
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        
        # Add handlers for text and photos only
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        
        logger.info("Bot started successfully")
        
        # Start polling
        application.run_polling()
        
    except Exception as e:
        logger.error(f"Error starting bot: {str(e)}")
        raise

if __name__ == '__main__':
    main()
