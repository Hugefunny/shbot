"""
图片词条机器人核心逻辑
功能:
  1. 存图: "有人问xx时回答" + 图片 → 保存图片到词条 xx
  2. 删图: "不要回答xx" → 删除该用户最后上传的图片
  3. 取图: "xx" (单独词条名) → 随机返回一张图片
  4. Dashboard 管理: 在 Web 后台管理图片词条
"""

import os
import re
import random
import time
from ErisPulse import sdk
from ErisPulse.Core.Bases import BaseModule
from ErisPulse.Core.Event import message, Event
from ErisPulse.Core import client
from fastapi.responses import JSONResponse, Response
from fastapi import Request, UploadFile

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

    def __init__(self):
        self.sdk = sdk
        self.logger = sdk.logger.get_child("Shbot")
        self._view_registered = False

    @staticmethod
    def get_load_strategy():
        from ErisPulse.loaders import ModuleLoadStrategy
        return ModuleLoadStrategy(lazy_load=False, priority=50)

    async def on_load(self, event):
        """模块加载时执行"""
        os.makedirs(IMAGES_DIR, exist_ok=True)
        self._register_routes()
        self._register_dashboard_view()
        sdk.logger.info(f"图片词条机器人已加载，存储目录: {os.path.abspath(IMAGES_DIR)}")

    async def on_unload(self, event):
        """模块卸载时执行"""
        self._unregister_routes()
        if self._view_registered:
            try:
                if hasattr(sdk, 'Dashboard') and sdk.Dashboard:
                    sdk.Dashboard.unregister_view("Shbot")
            except Exception:
                pass
        sdk.logger.info("图片词条机器人已卸载")

    # ==================== HTTP API 路由 ====================

    def _register_routes(self):
        """注册 HTTP API 路由"""
        r = sdk.router
        mn = "Shbot"

        r.register_http_route(mn, "/api/images", handler=self._api_list_tags, methods=["GET"])
        r.register_http_route(mn, "/api/images/{tag}", handler=self._api_get_tag_images, methods=["GET"])
        r.register_http_route(mn, "/api/images/{tag}", handler=self._api_upload_image, methods=["POST"])
        r.register_http_route(mn, "/api/images/{tag}/delete", handler=self._api_delete_image, methods=["POST"])
        r.register_http_route(mn, "/api/images/{tag}/clear", handler=self._api_clear_tag, methods=["POST"])
        r.register_http_route(mn, "/api/images/{tag}/delete-tag", handler=self._api_delete_tag, methods=["POST"])
        r.register_http_route(mn, "/api/images/file/{filename:path}", handler=self._api_serve_image, methods=["GET"])

        self.logger.info("Shbot HTTP API 路由已注册")

    def _unregister_routes(self):
        """注销 HTTP API 路由"""
        r = sdk.router
        routes = [
            "/api/images",
            "/api/images/{tag}",
            "/api/images/{tag}",
            "/api/images/{tag}/delete",
            "/api/images/{tag}/clear",
            "/api/images/{tag}/delete-tag",
            "/api/images/file/{filename:path}",
        ]
        for route in routes:
            try:
                r.unregister_http_route("Shbot", route)
            except Exception:
                pass

    def _verify_token(self, request: Request) -> bool:
        """验证 Dashboard Token"""
        if hasattr(sdk, 'Dashboard') and sdk.Dashboard:
            return sdk.Dashboard.verify_request(request)
        # 没有 Dashboard 时放行（开发环境）
        return True

    async def _api_list_tags(self, request: Request):
        """获取所有词条列表"""
        if not self._verify_token(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        tags = _get_tags()
        result = []
        for tag_name, images in tags.items():
            valid_count = sum(1 for img in images if os.path.exists(img.get("filepath", "")))
            result.append({
                "name": tag_name,
                "total": len(images),
                "valid": valid_count,
            })
        result.sort(key=lambda x: x["name"])
        return JSONResponse({"tags": result})

    async def _api_get_tag_images(self, request: Request):
        """获取指定词条下的所有图片"""
        if not self._verify_token(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        tag = request.path_params.get("tag", "")
        tags = _get_tags()
        images = tags.get(tag, [])
        result = []
        for i, img in enumerate(images):
            filename = os.path.basename(img.get("filepath", ""))
            exists = os.path.exists(img.get("filepath", ""))
            result.append({
                "index": i,
                "filename": filename,
                "uploader": img.get("uploader", ""),
                "time": img.get("time", 0),
                "exists": exists,
                "url": f"/Shbot/api/images/file/{filename}" if exists else None,
            })
        return JSONResponse({"tag": tag, "images": result, "total": len(result)})

    async def _api_upload_image(self, request: Request):
        """上传图片到指定词条"""
        if not self._verify_token(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        tag = request.path_params.get("tag", "")
        if not tag:
            return JSONResponse({"error": "词条名不能为空"}, status_code=400)

        uploader = request.headers.get("X-Uploader", "dashboard")
        form = await request.form()
        file: UploadFile | None = form.get("file")

        if not file or not file.filename:
            return JSONResponse({"error": "请选择要上传的图片"}, status_code=400)

        # 读取文件内容
        image_data = await file.read()
        timestamp = int(time.time())

        # 确定扩展名
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            ext = ".jpg"

        filename = f"{tag}_{timestamp}_dashboard{ext}"
        filepath = os.path.join(os.path.abspath(IMAGES_DIR), filename)

        with open(filepath, "wb") as f:
            f.write(image_data)

        sdk.logger.info(f"Dashboard 上传图片: {filepath}")

        tags = _get_tags()
        if tag not in tags:
            tags[tag] = []
        tags[tag].append({
            "filepath": filepath,
            "uploader": uploader,
            "time": timestamp,
        })
        _save_tags(tags)

        return JSONResponse({"success": True, "filename": filename})

    async def _api_delete_image(self, request: Request):
        """删除指定词条下的某张图片"""
        if not self._verify_token(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        tag = request.path_params.get("tag", "")
        body = await request.json()
        index = body.get("index", -1)

        tags = _get_tags()
        if tag not in tags or index < 0 or index >= len(tags[tag]):
            return JSONResponse({"error": "无效的索引"}, status_code=400)

        removed = tags[tag].pop(index)
        filepath = removed.get("filepath", "")
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError as e:
                self.logger.warning(f"删除图片文件失败: {e}")

        if not tags[tag]:
            del tags[tag]
        else:
            tags[tag] = tags[tag]
        _save_tags(tags)

        return JSONResponse({"success": True})

    async def _api_clear_tag(self, request: Request):
        """清空指定词条下的所有图片"""
        if not self._verify_token(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        tag = request.path_params.get("tag", "")
        tags = _get_tags()
        if tag not in tags:
            return JSONResponse({"error": "词条不存在"}, status_code=404)

        images = tags[tag]
        for img in images:
            filepath = img.get("filepath", "")
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass

        del tags[tag]
        _save_tags(tags)

        return JSONResponse({"success": True, "deleted": len(images)})

    async def _api_delete_tag(self, request: Request):
        """删除整个词条（含图片文件）"""
        if not self._verify_token(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        tag = request.path_params.get("tag", "")
        tags = _get_tags()
        if tag not in tags:
            return JSONResponse({"error": "词条不存在"}, status_code=404)

        images = tags[tag]
        for img in images:
            filepath = img.get("filepath", "")
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass

        del tags[tag]
        _save_tags(tags)

        return JSONResponse({"success": True, "deleted": len(images)})

    async def _api_serve_image(self, request: Request):
        """提供图片文件访问"""
        filename = request.path_params.get("filename", "")
        safe_name = os.path.basename(filename)
        filepath = os.path.join(os.path.abspath(IMAGES_DIR), safe_name)

        if not os.path.exists(filepath):
            return JSONResponse({"error": "File not found"}, status_code=404)

        ext = os.path.splitext(safe_name)[1].lower()
        media_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        media_type = media_types.get(ext, "application/octet-stream")

        with open(filepath, "rb") as f:
            content = f.read()

        return Response(content=content, media_type=media_type)

    # ==================== Dashboard 视窗注册 ====================

    def _register_dashboard_view(self):
        """注册 Dashboard 管理视窗"""
        try:
            if not hasattr(sdk, 'Dashboard') or not sdk.Dashboard:
                self.logger.warning("Dashboard 模块未加载，跳过视窗注册")
                return

            dashboard = sdk.Dashboard
            dashboard.register_view(
                id="Shbot",
                title="图片词条",
                title_en="Image Tags",
                icon_svg='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
                html_content=self._get_view_html(),
                js_content="",
                css_content=self._get_view_css(),
                loader="",
                group="group_tools",
                group_title="工具",
                group_title_en="Tools",
            )
            self._view_registered = True
            self.logger.info("Dashboard 视窗已注册")
        except Exception as e:
            self.logger.warning(f"注册 Dashboard 视窗失败: {e}")

    def _get_view_html(self) -> str:
        """返回 Dashboard 视窗的 HTML 内容（含内联 JS）"""
        return '''
        <div class="shbot-dashboard">
            <div class="shbot-header">
                <h1 class="page-title">图片词条管理</h1>
                <p style="color:var(--tx-s);margin-bottom:0">管理所有图片词条，支持上传、删除、查看图片</p>
            </div>

            <div class="shbot-layout">
                <!-- 侧边栏：词条列表 -->
                <div class="shbot-sidebar">
                    <div class="shbot-sidebar-header">
                        <span class="shbot-sidebar-title">词条列表</span>
                        <button class="btn btn-primary btn-sm" id="shbotBtnAddTag">+ 新建</button>
                    </div>
                    <div class="shbot-search-box">
                        <input type="text" class="shbot-search-input" id="shbotSearchInput" placeholder="搜索词条..." />
                    </div>
                    <div class="shbot-tag-list" id="shbotTagList">
                        <div class="shbot-loading" id="shbotTagLoading">加载中...</div>
                    </div>
                </div>

                <!-- 主内容区：图片展示 -->
                <div class="shbot-main" id="shbotMain">
                    <div class="shbot-empty" id="shbotEmpty">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="width:64px;height:64px;opacity:0.3">
                            <rect x="3" y="3" width="18" height="18" rx="2"/>
                            <circle cx="8.5" cy="8.5" r="1.5"/>
                            <path d="M21 15l-5-5L5 21"/>
                        </svg>
                        <p style="color:var(--tx-t);margin-top:12px">请从左侧选择一个词条</p>
                    </div>
                </div>
            </div>
        </div>

        <!-- 新建词条弹窗 -->
        <div class="shbot-modal-overlay" id="shbotModalOverlay" style="display:none">
            <div class="shbot-modal" onclick="event.stopPropagation()">
                <div class="shbot-modal-header">
                    <span class="shbot-modal-title" id="shbotModalTitle">新建词条</span>
                    <button class="btn-icon" id="shbotBtnCloseModal">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                    </button>
                </div>
                <div class="shbot-modal-body">
                    <label class="shbot-label">词条名称</label>
                    <input type="text" class="shbot-input" id="shbotNewTagName" placeholder="输入词条名称" />
                    <div id="shbotModalError" style="color:var(--er-c);font-size:13px;margin-top:8px;display:none"></div>
                </div>
                <div class="shbot-modal-footer">
                    <button class="btn btn-secondary" id="shbotBtnCancelModal">取消</button>
                    <button class="btn btn-primary" id="shbotBtnConfirmAddTag">确定</button>
                </div>
            </div>
        </div>

        <script>
        /* ---- 全局状态 ---- */
        var shbotState = { tags: [], currentTag: null, filteredTags: [], _token: function() { return localStorage.getItem("__ep_tk__") || ""; } };

        function shbotInit() {
            /* 绑定事件（避免 onclick 属性造成的全局函数依赖） */
            document.getElementById("shbotBtnAddTag").onclick = shbotShowAddTagModal;
            document.getElementById("shbotBtnCloseModal").onclick = shbotCloseModal;
            document.getElementById("shbotBtnCancelModal").onclick = shbotCloseModal;
            document.getElementById("shbotBtnConfirmAddTag").onclick = shbotConfirmAddTag;
            document.getElementById("shbotSearchInput").oninput = function() { shbotFilterTags(this.value); };
            document.getElementById("shbotModalOverlay").onclick = shbotCloseModal;
            shbotRefreshTagList();
        }

        /* ---- 词条列表 ---- */
        async function shbotRefreshTagList() {
            try {
                var resp = await fetch("/Shbot/api/images", {
                    headers: { "Authorization": "Bearer " + shbotState._token() }
                });
                var data = await resp.json();
                shbotState.tags = data.tags || [];
                shbotState.filteredTags = shbotState.tags;
                shbotRenderTagList();
            } catch(e) {
                console.error("Failed to load tags", e);
            }
        }

        function shbotRenderTagList() {
            var el = document.getElementById("shbotTagList");
            var list = shbotState.filteredTags;
            if (!list || list.length === 0) {
                el.innerHTML = '<div class="shbot-loading">\u6682\u65e0\u8bcd\u6761</div>';
                return;
            }
            el.innerHTML = list.map(function(t) {
                var active = shbotState.currentTag === t.name ? " active" : "";
                var safeName = shbotEscAttr(t.name);
                return '<div class="shbot-tag-item' + active + '" data-tagname="' + safeName + '">' +
                    '<span class="shbot-tag-name">' + shbotEscHtml(t.name) + '</span>' +
                    '<span class="shbot-tag-count">' + t.valid + "/" + t.total + "</span>" +
                    "</div>";
            }).join("");
            /* 绑定点击事件 */
            el.querySelectorAll(".shbot-tag-item").forEach(function(item) {
                item.onclick = function() {
                    shbotSelectTag(item.getAttribute("data-tagname"), item);
                };
            });
        }

        function shbotFilterTags(query) {
            if (!query) {
                shbotState.filteredTags = shbotState.tags;
            } else {
                var q = query.toLowerCase();
                shbotState.filteredTags = shbotState.tags.filter(function(t) {
                    return t.name.toLowerCase().indexOf(q) !== -1;
                });
            }
            shbotRenderTagList();
        }

        function shbotSelectTag(tagName, el) {
            document.querySelectorAll(".shbot-tag-item").forEach(function(e) { e.classList.remove("active"); });
            if (el) el.classList.add("active");
            shbotState.currentTag = tagName;
            shbotLoadTagImages(tagName);
        }

        /* ---- 图片展示 ---- */
        async function shbotLoadTagImages(tagName) {
            var main = document.getElementById("shbotMain");
            main.innerHTML = '<div class="shbot-tag-detail" id="shbotTagDetail"></div>';
            var detail = document.getElementById("shbotTagDetail");

            try {
                var resp = await fetch("/Shbot/api/images/" + encodeURIComponent(tagName), {
                    headers: { "Authorization": "Bearer " + shbotState._token() }
                });
                var data = await resp.json();
                var images = data.images || [];

                var headerHtml = '<div class="shbot-tag-detail-header">' +
                    '<span class="shbot-tag-detail-title">' + shbotEscHtml(tagName) + ' (' + images.length + ' \u5f20)</span>' +
                    '<div class="shbot-tag-detail-actions">' +
                    '<button class="btn btn-danger btn-sm" id="shbotBtnDeleteTag">\u5220\u9664\u8bcd\u6761</button>' +
                    '</div></div>';

                var gridHtml = '<div class="shbot-image-grid" id="shbotImageGrid">';
                if (images.length === 0) {
                    gridHtml += '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--tx-t)">\u6682\u65e0\u56fe\u7247</div>';
                } else {
                    images.forEach(function(img) {
                        gridHtml += '<div class="shbot-image-card">';
                        if (img.exists && img.url) {
                            gridHtml += '<img src="' + img.url + '" alt="' + shbotEscHtml(img.filename) + '" loading="lazy" />';
                        } else {
                            gridHtml += '<div style="height:140px;display:flex;align-items:center;justify-content:center;color:var(--er-c);font-size:12px">\u6587\u4ef6\u5df2\u4e22\u5931</div>';
                        }
                        gridHtml += '<button class="shbot-image-delete" data-index="' + img.index + '" title="\u5220\u9664">' +
                            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>' +
                            "</button>";
                        gridHtml += '<div class="shbot-image-info">';
                        gridHtml += '<span class="shbot-image-time">' + (img.time ? new Date(img.time * 1000).toLocaleString() : "") + "</span>";
                        gridHtml += "</div></div>";
                    });
                }
                gridHtml += "</div>";

                var uploadHtml = '<div class="shbot-upload-area">' +
                    '<label class="shbot-upload-btn" id="shbotUploadBtn">' +
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>' +
                    '\u4e0a\u4f20\u56fe\u7247' +
                    '<input type="file" accept="image/*" id="shbotFileInput" />' +
                    '</label>' +
                    '</div>';

                detail.innerHTML = headerHtml + gridHtml + uploadHtml;

                /* 绑定事件 */
                var deleteBtn = document.getElementById("shbotBtnDeleteTag");
                if (deleteBtn) deleteBtn.onclick = function() { shbotConfirmDeleteTag(tagName); };

                var fileInput = document.getElementById("shbotFileInput");
                if (fileInput) fileInput.onchange = function() { shbotUploadImage(this, tagName); };

                document.querySelectorAll(".shbot-image-delete").forEach(function(btn) {
                    btn.onclick = function() { shbotDeleteImage(parseInt(this.getAttribute("data-index"))); };
                });
            } catch(e) {
                detail.innerHTML = '<div class="shbot-empty"><p style="color:var(--er-c)">\u52a0\u8f7d\u5931\u8d25: ' + e.message + "</p></div>";
            }
        }

        /* ---- 上传图片 ---- */
        async function shbotUploadImage(input, tagName) {
            var file = input.files[0];
            if (!file) return;

            var formData = new FormData();
            formData.append("file", file);

            try {
                var resp = await fetch("/Shbot/api/images/" + encodeURIComponent(tagName), {
                    method: "POST",
                    headers: { "Authorization": "Bearer " + shbotState._token() },
                    body: formData
                });
                var data = await resp.json();
                if (data.success) {
                    shbotShowToast("\u4e0a\u4f20\u6210\u529f", "success");
                    input.value = "";
                    shbotLoadTagImages(tagName);
                    shbotRefreshTagList();
                } else {
                    shbotShowToast(data.error || "\u4e0a\u4f20\u5931\u8d25", "error");
                }
            } catch(e) {
                shbotShowToast("\u4e0a\u4f20\u5931\u8d25: " + e.message, "error");
            }
        }

        /* ---- 删除图片 ---- */
        async function shbotDeleteImage(index) {
            if (!shbotState.currentTag) return;
            if (!confirm("\u786e\u5b9a\u8981\u5220\u9664\u8fd9\u5f20\u56fe\u7247\u5417\uff1f")) return;

            try {
                var resp = await fetch("/Shbot/api/images/" + encodeURIComponent(shbotState.currentTag) + "/delete", {
                    method: "POST",
                    headers: {
                        "Authorization": "Bearer " + shbotState._token(),
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({ index: index })
                });
                var data = await resp.json();
                if (data.success) {
                    shbotShowToast("\u5220\u9664\u6210\u529f", "success");
                    shbotLoadTagImages(shbotState.currentTag);
                    shbotRefreshTagList();
                } else {
                    shbotShowToast(data.error || "\u5220\u9664\u5931\u8d25", "error");
                }
            } catch(e) {
                shbotShowToast("\u5220\u9664\u5931\u8d25: " + e.message, "error");
            }
        }

        /* ---- 删除词条 ---- */
        function shbotConfirmDeleteTag(tagName) {
            if (!confirm("\u786e\u5b9a\u8981\u5220\u9664\u8bcd\u6761\u300c" + tagName + "\u300d\u53ca\u5176\u6240\u6709\u56fe\u7247\u5417\uff1f\u6b64\u64cd\u4f5c\u4e0d\u53ef\u64a4\u9500\u3002")) return;
            shbotDeleteTag(tagName);
        }

        async function shbotDeleteTag(tagName) {
            try {
                var resp = await fetch("/Shbot/api/images/" + encodeURIComponent(tagName) + "/delete-tag", {
                    method: "POST",
                    headers: {
                        "Authorization": "Bearer " + shbotState._token(),
                        "Content-Type": "application/json"
                    }
                });
                var data = await resp.json();
                if (data.success) {
                    shbotShowToast("\u8bcd\u6761\u5df2\u5220\u9664", "success");
                    shbotState.currentTag = null;
                    document.getElementById("shbotMain").innerHTML = '<div class="shbot-empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="width:64px;height:64px;opacity:0.3"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg><p style="color:var(--tx-t);margin-top:12px">\u8bf7\u4ece\u5de6\u4fa7\u9009\u62e9\u4e00\u4e2a\u8bcd\u6761</p></div>';
                    shbotRefreshTagList();
                } else {
                    shbotShowToast(data.error || "\u5220\u9664\u5931\u8d25", "error");
                }
            } catch(e) {
                shbotShowToast("\u5220\u9664\u5931\u8d25: " + e.message, "error");
            }
        }

        /* ---- 弹窗 ---- */
        function shbotShowAddTagModal() {
            document.getElementById("shbotModalOverlay").style.display = "flex";
            document.getElementById("shbotNewTagName").value = "";
            document.getElementById("shbotModalError").style.display = "none";
            document.getElementById("shbotNewTagName").focus();
        }

        function shbotCloseModal() {
            document.getElementById("shbotModalOverlay").style.display = "none";
        }

        function shbotConfirmAddTag() {
            var name = document.getElementById("shbotNewTagName").value.trim();
            if (!name) {
                shbotShowModalError("\u8bcd\u6761\u540d\u79f0\u4e0d\u80fd\u4e3a\u7a7a");
                return;
            }
            var exists = shbotState.tags.some(function(t) { return t.name === name; });
            if (exists) {
                shbotShowModalError("\u8bcd\u6761\u300c" + name + "\u300d\u5df2\u5b58\u5728");
                return;
            }
            shbotCloseModal();
            shbotState.currentTag = name;
            shbotRefreshTagList();
            shbotShowToast("\u65b0\u5efa\u8bcd\u6761\u300c" + name + "\u300d", "success");
            setTimeout(function() {
                var items = document.querySelectorAll(".shbot-tag-item");
                items.forEach(function(el) {
                    if (el.getAttribute("data-tagname") === name) {
                        shbotSelectTag(name, el);
                    }
                });
            }, 300);
        }

        function shbotShowModalError(msg) {
            var el = document.getElementById("shbotModalError");
            el.textContent = msg;
            el.style.display = "block";
        }

        /* ---- Toast ---- */
        function shbotShowToast(msg, type) {
            var existing = document.querySelector(".shbot-toast");
            if (existing) existing.remove();
            var toast = document.createElement("div");
            toast.className = "shbot-toast shbot-toast-" + (type || "success");
            toast.textContent = msg;
            document.body.appendChild(toast);
            setTimeout(function() { toast.remove(); }, 2500);
        }

        /* ---- 工具函数 ---- */
        function shbotEscHtml(str) {
            if (!str) return "";
            return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
        }

        function shbotEscAttr(str) {
            if (!str) return "";
            return String(str).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
        }

        /* ---- 等待 DOM 就绪后初始化 ---- */
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", shbotInit);
        } else {
            shbotInit();
        }
        </script>
        '''

    def _get_view_css(self) -> str:
        """返回 Dashboard 视窗的自定义 CSS"""
        return '''
        .shbot-dashboard { height:100%; display:flex; flex-direction:column; }
        .shbot-header { margin-bottom:16px; flex-shrink:0; }
        .shbot-layout { display:flex; gap:16px; flex:1; min-height:0; }
        .shbot-sidebar { width:280px; flex-shrink:0; display:flex; flex-direction:column; background:var(--bg-s); border-radius:8px; border:1px solid var(--bd); overflow:hidden; }
        .shbot-sidebar-header { display:flex; align-items:center; justify-content:space-between; padding:12px 14px; border-bottom:1px solid var(--bd); }
        .shbot-sidebar-title { font-size:14px; font-weight:600; color:var(--tx-p); }
        .shbot-search-box { padding:8px 14px; border-bottom:1px solid var(--bd); }
        .shbot-search-input { width:100%; padding:7px 10px; border:1px solid var(--bd); border-radius:6px; background:var(--bg-p); color:var(--tx-p); font-size:13px; outline:none; box-sizing:border-box; }
        .shbot-search-input:focus { border-color:var(--accent); }
        .shbot-tag-list { flex:1; overflow-y:auto; padding:6px; }
        .shbot-tag-item { display:flex; align-items:center; justify-content:space-between; padding:8px 10px; border-radius:6px; cursor:pointer; transition:background .15s; margin-bottom:2px; }
        .shbot-tag-item:hover { background:var(--bg-t); }
        .shbot-tag-item.active { background:var(--accent); color:#fff; }
        .shbot-tag-item.active .shbot-tag-count { color:rgba(255,255,255,0.7); }
        .shbot-tag-name { font-size:13px; font-weight:500; }
        .shbot-tag-count { font-size:11px; color:var(--tx-t); margin-left:8px; }
        .shbot-main { flex:1; display:flex; flex-direction:column; background:var(--bg-s); border-radius:8px; border:1px solid var(--bd); overflow:hidden; }
        .shbot-empty { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; }
        .shbot-tag-detail { flex:1; display:flex; flex-direction:column; overflow:hidden; }
        .shbot-tag-detail-header { display:flex; align-items:center; justify-content:space-between; padding:14px 18px; border-bottom:1px solid var(--bd); flex-shrink:0; }
        .shbot-tag-detail-title { font-size:15px; font-weight:600; color:var(--tx-p); }
        .shbot-tag-detail-actions { display:flex; gap:8px; align-items:center; }
        .shbot-image-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:12px; padding:16px; overflow-y:auto; flex:1; align-content:start; }
        .shbot-image-card { position:relative; background:var(--bg-t); border-radius:8px; overflow:hidden; border:1px solid var(--bd); transition:transform .15s,box-shadow .15s; }
        .shbot-image-card:hover { transform:translateY(-2px); box-shadow:0 4px 12px rgba(0,0,0,0.1); }
        .shbot-image-card img { width:100%; height:140px; object-fit:cover; display:block; }
        .shbot-image-info { padding:8px 10px; font-size:11px; color:var(--tx-s); }
        .shbot-image-info .shbot-image-uploader { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .shbot-image-info .shbot-image-time { display:block; color:var(--tx-t); margin-top:2px; }
        .shbot-image-delete { position:absolute; top:6px; right:6px; width:26px;height:26px; border-radius:50%; background:rgba(0,0,0,0.5); border:none; color:#fff; cursor:pointer; display:flex;align-items:center;justify-content:center; opacity:0; transition:opacity .15s; }
        .shbot-image-card:hover .shbot-image-delete { opacity:1; }
        .shbot-image-delete:hover { background:var(--er-c); }
        .shbot-upload-area { padding:12px 18px; border-top:1px solid var(--bd); flex-shrink:0; display:flex; gap:10px; align-items:center; }
        .shbot-upload-area input[type="file"] { display:none; }
        .shbot-upload-btn { display:inline-flex; align-items:center; gap:6px; padding:7px 14px; border-radius:6px; border:1px dashed var(--bd); cursor:pointer; font-size:13px; color:var(--tx-s); transition:all .15s; }
        .shbot-upload-btn:hover { border-color:var(--accent); color:var(--accent); }
        .shbot-loading { text-align:center; padding:24px; color:var(--tx-t); font-size:13px; }
        .btn-sm { padding:5px 12px; font-size:12px; }
        .shbot-modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.4); display:flex; align-items:center; justify-content:center; z-index:1000; }
        .shbot-modal { background:var(--bg-p); border-radius:12px; width:400px; max-width:90vw; box-shadow:0 20px 60px rgba(0,0,0,0.3); }
        .shbot-modal-header { display:flex; align-items:center; justify-content:space-between; padding:16px 20px; border-bottom:1px solid var(--bd); }
        .shbot-modal-title { font-size:15px; font-weight:600; color:var(--tx-p); }
        .shbot-modal-body { padding:16px 20px; }
        .shbot-modal-footer { display:flex; justify-content:flex-end; gap:8px; padding:12px 20px; border-top:1px solid var(--bd); }
        .shbot-label { display:block; font-size:13px; font-weight:500; color:var(--tx-p); margin-bottom:6px; }
        .shbot-input { width:100%; padding:8px 12px; border:1px solid var(--bd); border-radius:6px; background:var(--bg-s); color:var(--tx-p); font-size:13px; outline:none; box-sizing:border-box; }
        .shbot-input:focus { border-color:var(--accent); }
        .shbot-toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%); padding:10px 20px; border-radius:8px; font-size:13px; z-index:2000; animation:shbotFadeIn .3s; }
        .shbot-toast-success { background:var(--ok-c); color:#fff; }
        .shbot-toast-error { background:var(--er-c); color:#fff; }
        @keyframes shbotFadeIn { from { opacity:0; transform:translateX(-50%) translateY(10px); } to { opacity:1; transform:translateX(-50%) translateY(0); } }
        '''

    def _get_view_js(self) -> str:
        """JS 已内联到 HTML 中"""
        return ""