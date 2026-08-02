import re
import time
import random
from ErisPulse import sdk
from ErisPulse.Core.Bases import BaseModule
from ErisPulse.Core.Event import message, Event


class Main(BaseModule):
    def __init__(self):
        self.sdk = sdk
        self.logger = sdk.logger.get_child("ImageBot")
        self.storage = sdk.storage

    @staticmethod
    def get_load_strategy():
        from ErisPulse.loaders import ModuleLoadStrategy
        return ModuleLoadStrategy(
            lazy_load=False,
            priority=0
        )

    async def on_load(self, event):
        @message.on_group_message()
        async def group_msg_handler(event: Event):
            if event.get_platform() != "qqbot":
                return

            text = self._strip_mention(event.get_text()).strip()
            if not text:
                return

            has_image = self._has_image(event)

            # Pattern 1: 有人问{keyword}时回答 + image → store image
            match = re.match(r'^有人问(.+)时回答$', text)
            if match:
                keyword = match.group(1).strip()
                if not keyword:
                    return
                if has_image:
                    image_url = self._get_image_url(event)
                    if image_url:
                        await self._store_image(keyword, image_url, event.get_user_id())
                        await event.reply(f"已保存「{keyword}」的图片")
                    else:
                        await event.reply("无法获取图片，请重试")
                else:
                    await event.reply("回答呢？回答呢？")
                return

            # Pattern 2: 不要回答{keyword} → delete last image by this user
            match = re.match(r'^不要回答(.+)$', text)
            if match:
                keyword = match.group(1).strip()
                if not keyword:
                    return
                deleted = await self._delete_last_image(keyword, event.get_user_id())
                if deleted:
                    await event.reply(f"已删除「{keyword}」的最后一张图片")
                else:
                    await event.reply(f"「{keyword}」没有可删除的图片")
                return

            # Pattern 3: {keyword} → send random image from gallery
            images = await self._get_tag_images(text)
            if images:
                image_url = random.choice(images)["url"]
                await event.reply(image_url, method="Image")
                return

        self.logger.info("图片存储模块已加载")

    async def on_unload(self, event):
        self.logger.info("图片存储模块已卸载")

    # ---------- helpers ----------

    def _strip_mention(self, text: str) -> str:
        """Remove @mention prefixes from text."""
        text = re.sub(r'<@!?\w+>', '', text)
        text = re.sub(r'@\S+', '', text)
        return text.strip()

    def _has_image(self, event: Event) -> bool:
        """Check if the event contains an image segment."""
        msg = event.get_message()
        return any(s.get("type") == "image" for s in msg)

    def _get_image_url(self, event: Event) -> str | None:
        """Extract the first image URL from the event."""
        msg = event.get_message()
        for s in msg:
            if s.get("type") == "image":
                data = s.get("data", {})
                return data.get("url") or data.get("file")
        return None

    async def _store_image(self, tag: str, url: str, user_id: str):
        """Append an image to the tag's gallery."""
        key = f"img_tags:{tag}"
        images = self.storage.get(key, [])
        images.append({
            "url": url,
            "uploader": user_id,
            "time": time.time()
        })
        self.storage.set(key, images)

    async def _delete_last_image(self, tag: str, user_id: str) -> bool:
        """Remove the last image uploaded by this user for the tag."""
        key = f"img_tags:{tag}"
        images = self.storage.get(key, [])
        for i in range(len(images) - 1, -1, -1):
            if images[i]["uploader"] == user_id:
                del images[i]
                self.storage.set(key, images)
                return True
        return False

    async def _get_tag_images(self, tag: str) -> list[dict] | None:
        """Return the image list for a tag, or None if empty."""
        key = f"img_tags:{tag}"
        images = self.storage.get(key, [])
        return images if images else None