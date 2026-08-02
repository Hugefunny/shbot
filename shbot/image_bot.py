"""
图片词条机器人核心逻辑
功能:
  1. 存图: "有人问xx时回答" + 图片 → 保存图片到词条 xx
  2. 删图: "不要回答xx" → 删除该用户最后上传的图片
  3. 取图: "xx" (单独词条名) → 随机返回一张图片
"""

import os
import re
import random
import time
from ErisPulse import sdk
from ErisPulse.Core.Bases import BaseModule
from ErisPulse.Core.Event import message, Event
from ErisPulse.Core import client

# ==================== 配置 ====================

IMAGES_DIR = "images"
PATTERN_SAVE = re.compile(r"^有人问(.+)时回答$")
PATTERN_DELETE = re.compile(r"^不要回答(.+)$")


# ==================== 存储操作 ====================

def _get_tags() -> dict:
    """获取所有词条数据"""
    return sdk.storage.get("image_tags", {})


def _save_tags(tags: dict):
    """保存词条数据"""
    sdk.storage.set("image_tags", tags)


# ==================== 图片处理 ====================

async def _download_image(image_url: str, tag_name: str, user_id: str) -> str | None:
    """下载图片并保存到本地，返回文件路径"""
    try:
        resp = await client.get(image_url)
        if resp.status != 200:
            sdk.logger.error(f"图片下载失败, status={resp.status}, url={image_url}")
            return None

        image_data = await resp.read()
        timestamp = int(time.time())
        ext = ".jpg"
        if "png" in image_url.lower():
            ext = ".png"
        elif "gif" in image_url.lower():
            ext = ".gif"
        elif "webp" in image_url.lower():
            ext = ".webp"
        filename = f"{tag_name}_{timestamp}_{user_id}{ext}"
        filepath = os.path.join(os.path.abspath(IMAGES_DIR), filename)
        with open(filepath, "wb") as f:
            f.write(image_data)
        sdk.logger.info(f"图片已保存: {filepath}")
        return filepath
    except Exception as e:
        sdk.logger.error(f"图片下载异常: {e}")
        return None


def _get_image_url(event: Event) -> str | None:
    """从消息中提取第一张图片的 URL"""
    for segment in event.get_message():
        if segment.get("type") == "image":
            file_url = segment.get("data", {}).get("file")
            if file_url:
                return file_url
    return None


# ==================== 消息处理器 ====================

@message.on_group_message()
async def image_tag_handler(event: Event):
    """处理群聊中的图片词条相关消息"""
    text = event.get_text().strip()
    user_id = event.get_user_id()

    if not text:
        return

    # ========== 1. 存图模式: "有人问xx时回答" ==========
    match_save = PATTERN_SAVE.match(text)
    if match_save:
        tag_name = match_save.group(1).strip()
        if not tag_name:
            await event.reply("词条名不能为空")
            return

        image_url = _get_image_url(event)
        if not image_url:
            await event.reply("回答呢？回答呢？")
            return

        filepath = await _download_image(image_url, tag_name, user_id)
        if filepath is None:
            await event.reply("图片下载失败，请稍后重试")
            return

        tags = _get_tags()
        if tag_name not in tags:
            tags[tag_name] = []
        tags[tag_name].append({
            "filepath": filepath,
            "uploader": user_id,
            "time": int(time.time()),
        })
        _save_tags(tags)

        await event.reply(f"已保存图片到词条「{tag_name}」")
        return

    # ========== 2. 删图模式: "不要回答xx" ==========
    match_delete = PATTERN_DELETE.match(text)
    if match_delete:
        tag_name = match_delete.group(1).strip()
        if not tag_name:
            await event.reply("词条名不能为空")
            return

        tags = _get_tags()
        if tag_name not in tags or not tags[tag_name]:
            await event.reply(f"词条「{tag_name}」下没有图片")
            return

        images = tags[tag_name]
        found_idx = -1
        for i in range(len(images) - 1, -1, -1):
            if images[i].get("uploader") == user_id:
                found_idx = i
                break

        if found_idx == -1:
            await event.reply(f"你没有在词条「{tag_name}」下上传过图片")
            return

        removed = images.pop(found_idx)
        filepath = removed.get("filepath", "")
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
                sdk.logger.info(f"已删除图片文件: {filepath}")
            except OSError as e:
                sdk.logger.warning(f"删除图片文件失败: {e}")

        tags[tag_name] = images
        _save_tags(tags)
        await event.reply(f"已删除你在词条「{tag_name}」下的最后一张上传图片")
        return

    # ========== 3. 取图模式: 单独词条名 ==========
    tags = _get_tags()
    if text in tags and tags[text]:
        images = tags[text]
        valid_images = [img for img in images if os.path.exists(img.get("filepath", ""))]
        if not valid_images:
            await event.reply(f"词条「{text}」下的图片文件都已失效")
            return

        chosen = random.choice(valid_images)
        await event.reply(f"file:///{chosen['filepath']}", method="Image")


# ==================== 模块入口 ====================

class Main(BaseModule):
    """图片词条机器人模块"""

    @staticmethod
    def get_load_strategy():
        from ErisPulse.loaders import ModuleLoadStrategy
        return ModuleLoadStrategy(lazy_load=False, priority=0)

    async def on_load(self, event):
        """模块加载时执行"""
        os.makedirs(IMAGES_DIR, exist_ok=True)
        sdk.logger.info(f"图片词条机器人已加载，存储目录: {os.path.abspath(IMAGES_DIR)}")

    async def on_unload(self, event):
        """模块卸载时执行"""
        sdk.logger.info("图片词条机器人已卸载")