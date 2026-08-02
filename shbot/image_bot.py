"""
图片词条存储模块
支持「有人问xx时回答」存储图片、「不要回答xx」删除图片、「xx」随机发送图片
并提供 Dashboard 管理视窗
"""
import re
import time
import random
from typing import Any
from ErisPulse import sdk
from ErisPulse.Core.Bases import BaseModule
from ErisPulse.Core.Event import message, Event


class Main(BaseModule):
    """图片词条存储模块主类"""

    def __init__(self):
        self.sdk = sdk
        self.logger = sdk.logger.get_child("ImageBot")
        self.storage = sdk.storage
        self.view_id = "ImageBot"

    @staticmethod
    def get_load_strategy():
        """模块加载策略"""
        from ErisPulse.loaders import ModuleLoadStrategy
        return ModuleLoadStrategy(
            lazy_load=False,
            priority=50
        )

    async def on_load(self, event: dict[str, Any]) -> bool:
        """模块加载时注册消息处理器、API 路由与 Dashboard 视窗"""
        self._register_message_handler()
        # 路由注册依赖 web 栈，可能未安装，需容错
        try:
            self._register_routes()
        except Exception as e:
            self.logger.warning(f"API 路由注册失败（web 栈可能未启用）: {e}")
        self._register_dashboard_view()
        self.logger.info("图片词条存储模块已加载")
        return True

    async def on_unload(self, event: dict[str, Any]) -> bool:
        """模块卸载时清理资源"""
        self._unregister_routes()
        if hasattr(self.sdk, 'Dashboard') and self.sdk.Dashboard:
            self.sdk.Dashboard.unregister_view(self.view_id)
        self.logger.info("图片词条存储模块已卸载")
        return True

    # ---------- 消息处理 ----------

    def _register_message_handler(self):
        """注册通用消息处理器（群聊/私聊均响应）"""

        @message.on_message(priority=50)
        async def msg_handler(event: Event):
            # 只从 text 类型消息段拼接命令文本，避免 alt_message 中混入 [图片] 等占位符
            raw_text = self._get_raw_text(event)
            text = self._strip_mention(raw_text).strip()
            if not text:
                return

            images = self._extract_images(event)

            # Pattern 1: 有人问{tag}时回答 → 存储图片
            match = re.match(r'^有人问(.+?)时回答$', text)
            if match:
                tag = match.group(1).strip()
                if not tag:
                    return
                if not images:
                    await event.reply("回答呢？回答呢？")
                    return
                for url in images:
                    await self._store_image(tag, url, event.get_user_id())
                await event.reply(f"已保存「{tag}」的图片（共{len(images)}张）")
                return

            # Pattern 2: 不要回答{tag} → 删除该用户最后一张图片（可持续）
            match = re.match(r'^不要回答(.+)$', text)
            if match:
                tag = match.group(1).strip()
                if not tag:
                    return
                deleted_url = await self._delete_last_image(tag, event.get_user_id())
                if deleted_url:
                    await event.reply(f"已删除「{tag}」中你上传的最后一张图片")
                else:
                    await event.reply(f"「{tag}」下没有你上传的图片了")
                return

            # Pattern 3: {tag} → 随机发送一张图（精确匹配优先）
            entry = await self._get_entry(text)
            if entry:
                image = random.choice(entry)
                await self._send_image(event, image["url"])
                return

    # ---------- 图片提取（健壮实现） ----------

    def _get_raw_text(self, event: Event) -> str:
        """
        从消息段中提取纯文本
        仅拼接 type="text" 的消息段内容，不包含图片/表情等其它段的占位符
        """
        try:
            msg = event.get_message() or []
        except Exception:
            return ""
        parts = []
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                seg_data = seg.get("data", {}) or {}
                seg_text = seg_data.get("text") or seg_data.get("content") or ""
                if seg_text:
                    parts.append(seg_text)
        return "".join(parts)

    def _extract_images(self, event: Event) -> list[str]:
        """
        提取消息中的所有图片 URL
        兼容多种消息段格式（image / img 类型，url / file / path / src 字段）
        """
        urls: list[str] = []
        try:
            msg = event.get_message() or []
        except Exception:
            return urls

        for seg in msg:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") not in ("image", "img"):
                continue
            seg_data = seg.get("data", {}) or {}
            # 调试：打印图片段原始结构，便于定位 QQ 图片字段格式
            self.logger.info(f"图片段原始数据: {seg_data}")
            # 仅采用 http(s) URL 作为可用图片地址
            url = (
                seg_data.get("url")
                or seg_data.get("src")
            )
            if not url:
                # file/path 可能是 QQ 的 fileid 或 rkey 链接，需 http 才可用
                for cand in (seg_data.get("file"), seg_data.get("path")):
                    if isinstance(cand, str) and cand.startswith(("http://", "https://")):
                        url = cand
                        break
            if url and url not in urls:
                urls.append(url)
        return urls

    # ---------- 图片发送 ----------

    async def _send_image(self, event: Event, image_ref: str):
        """
        健壮地发送图片
        对于 http(s) URL，先下载转为 base64 发送，避免部分适配器将 URL 误当作本地路径
        :param image_ref: 图片引用（URL / 本地路径 / base64 字符串）
        """
        # 已经是 base64 或本地路径，直接发送
        if image_ref.startswith(("base64://", "file://")) or not image_ref.startswith(("http://", "https://")):
            try:
                await event.reply(image_ref, method="Image")
            except Exception as e:
                self.logger.error(f"发送图片失败: {e}")
            return

        # http(s) URL：下载后转 base64 发送
        try:
            resp = await self.sdk.client.get(image_ref, timeout=30)
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.read()
            if not data:
                raise RuntimeError("空响应体")
            import base64
            b64 = base64.b64encode(data).decode("ascii")
            await event.reply(f"base64://{b64}", method="Image")
        except Exception as e:
            self.logger.warning(f"下载图片转 base64 失败，回退直发 URL: {e}")
            try:
                await event.reply(image_ref, method="Image")
            except Exception as e2:
                self.logger.error(f"回退发送图片仍失败: {e2}")

    # ---------- 存储操作 ----------

    def _entry_key(self, tag: str) -> str:
        """获取词条在 storage 中的键名"""
        return f"img_entries:{tag}"

    async def _store_image(self, tag: str, url: str, user_id: str):
        """存储一张图片到指定词条（词条不存在则新建）"""
        key = self._entry_key(tag)
        entry = self.storage.get(key, [])
        entry.append({
            "url": url,
            "uploader": user_id,
            "time": time.time()
        })
        self.storage.set(key, entry)

    async def _delete_last_image(self, tag: str, user_id: str) -> str | None:
        """
        删除指定词条下该用户上传的最后一张图片
        可持续调用：每次都删除当前最后一张
        返回被删除的图片 URL，若无则返回 None
        """
        key = self._entry_key(tag)
        entry = self.storage.get(key, [])
        # 从后往前找该用户上传的第一张（即最后一张）
        for i in range(len(entry) - 1, -1, -1):
            if entry[i].get("uploader") == user_id:
                deleted = entry.pop(i)
                if entry:
                    self.storage.set(key, entry)
                else:
                    self.storage.delete(key)
                return deleted.get("url")
        return None

    async def _get_entry(self, tag: str) -> list[dict[str, Any]] | None:
        """获取词条下的所有图片，若为空则返回 None"""
        key = self._entry_key(tag)
        entry = self.storage.get(key, [])
        return entry or None

    async def _get_all_entries(self) -> dict[str, list[dict[str, Any]]]:
        """获取所有词条及其图片列表"""
        result: dict[str, list[dict[str, Any]]] = {}
        all_keys = self.storage.get_all_keys() or []
        prefix = "img_entries:"
        for key in all_keys:
            if key.startswith(prefix):
                tag = key[len(prefix):]
                entry = self.storage.get(key, [])
                result[tag] = entry
        return result

    def _strip_mention(self, text: str) -> str:
        """移除文本中的 @提及 前缀"""
        text = re.sub(r'<@!?\w+>', '', text)
        text = re.sub(r'@\S+', '', text)
        return text.strip()

    # ---------- API 路由 ----------

    def _register_routes(self):
        """注册 Dashboard 使用的 API 路由"""
        r = self.sdk.router
        r.register_http_route(
            "ImageBot", "/api/entries/list",
            handler=self._api_list_entries, methods=["GET"]
        )
        r.register_http_route(
            "ImageBot", "/api/entries/add",
            handler=self._api_add_image, methods=["POST"]
        )
        r.register_http_route(
            "ImageBot", "/api/entries/delete",
            handler=self._api_delete_entry, methods=["POST"]
        )
        r.register_http_route(
            "ImageBot", "/api/entries/images/delete",
            handler=self._api_delete_image, methods=["POST"]
        )
        r.register_http_route(
            "ImageBot", "/api/entries/detail",
            handler=self._api_entry_detail, methods=["GET"]
        )

    def _unregister_routes(self):
        """注销所有 API 路由"""
        r = self.sdk.router
        routes = [
            "/api/entries/list",
            "/api/entries/add",
            "/api/entries/delete",
            "/api/entries/images/delete",
            "/api/entries/detail",
        ]
        for route in routes:
            try:
                r.unregister_http_route("ImageBot", route)
            except Exception:
                pass

    async def _api_list_entries(self, request: Any):
        """API: 获取所有词条列表"""
        entries = await self._get_all_entries()
        result = []
        for tag, images in entries.items():
            uploaders = {}
            for img in images:
                uid = img.get("uploader", "unknown")
                uploaders[uid] = uploaders.get(uid, 0) + 1
            result.append({
                "tag": tag,
                "count": len(images),
                "uploaders": uploaders,
                "created_at": images[0].get("time") if images else None,
            })
        return {"entries": result}

    async def _api_add_image(self, request: Any):
        """API: 向指定词条添加图片"""
        try:
            data = await request.json()
            tag = (data.get("tag") or "").strip()
            url = (data.get("url") or "").strip()
            user_id = data.get("user_id", "dashboard")

            if not tag or not url:
                return {"error": "tag 和 url 不能为空"}, 400

            await self._store_image(tag, url, user_id)
            return {"success": True, "message": "添加成功"}
        except Exception as e:
            self.logger.error(f"添加图片失败: {e}")
            return {"error": str(e)}, 500

    async def _api_delete_entry(self, request: Any):
        """API: 删除整个词条"""
        try:
            data = await request.json()
            tag = (data.get("tag") or "").strip()
            if not tag:
                return {"error": "tag 不能为空"}, 400

            key = self._entry_key(tag)
            if self.storage.get(key) is not None:
                self.storage.delete(key)
                return {"success": True, "deleted": True}
            return {"success": True, "deleted": False}
        except Exception as e:
            self.logger.error(f"删除词条失败: {e}")
            return {"error": str(e)}, 500

    async def _api_entry_detail(self, request: Any):
        """API: 获取指定词条下的所有图片详情"""
        try:
            tag = (request.query_params.get("tag") or "").strip()
            if not tag:
                return {"error": "tag 参数不能为空"}, 400
            entry = await self._get_entry(tag)
            return {"tag": tag, "images": entry or []}
        except Exception as e:
            self.logger.error(f"获取词条详情失败: {e}")
            return {"error": str(e)}, 500

    async def _api_delete_image(self, request: Any):
        """API: 删除词条中的指定图片（按索引）"""
        try:
            data = await request.json()
            tag = (data.get("tag") or "").strip()
            index = int(data.get("index", -1))
            if not tag or index < 0:
                return {"error": "参数不完整"}, 400

            key = self._entry_key(tag)
            entry = self.storage.get(key, [])
            if 0 <= index < len(entry):
                entry.pop(index)
                if entry:
                    self.storage.set(key, entry)
                else:
                    self.storage.delete(key)
                return {"success": True}
            return {"error": "索引越界"}, 400
        except Exception as e:
            self.logger.error(f"删除图片失败: {e}")
            return {"error": str(e)}, 500

    # ---------- Dashboard 视窗 ----------

    def _register_dashboard_view(self):
        """注册 Dashboard 管理视窗"""
        try:
            dashboard = self.sdk.Dashboard
            dashboard.register_view(
                id=self.view_id,
                title="图片词条", title_en="Image Entries",
                icon_svg='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
                html_content='''
                    <h1 class="page-title">图片词条管理</h1>
                    <p style="color:var(--tx-s);margin-bottom:16px">管理「有人问xx时回答」存储的图片词条</p>

                    <div class="grid-2">
                        <!-- 添加图片 -->
                        <div class="card">
                            <div class="card-header">添加图片</div>
                            <div class="card-body">
                                <div style="margin-bottom:12px">
                                    <label style="display:block;margin-bottom:4px;color:var(--tx-p)">词条</label>
                                    <input type="text" id="ib-tag-input" class="btn" style="width:100%;padding:8px;border:1px solid var(--bd);background:var(--bg-p)" placeholder="如：猫">
                                </div>
                                <div style="margin-bottom:12px">
                                    <label style="display:block;margin-bottom:4px;color:var(--tx-p)">图片 URL</label>
                                    <input type="text" id="ib-url-input" class="btn" style="width:100%;padding:8px;border:1px solid var(--bd);background:var(--bg-p)" placeholder="https://...">
                                </div>
                                <button class="btn btn-primary" style="width:100%" onclick="ibAdd()">添加</button>
                            </div>
                        </div>

                        <!-- 词条列表 -->
                        <div class="card">
                            <div class="card-header">词条列表</div>
                            <div class="card-body">
                                <div style="margin-bottom:12px">
                                    <input type="text" id="ib-search" class="btn" style="width:calc(100% - 80px);padding:8px;border:1px solid var(--bd);background:var(--bg-p)" placeholder="搜索词条..." onkeyup="ibFilter()">
                                    <button class="btn btn-secondary" onclick="ibRefresh()">刷新</button>
                                </div>
                                <div id="ib-list" style="max-height:300px;overflow-y:auto">
                                    <p style="color:var(--tx-t)">点击刷新加载</p>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- 图片详情 -->
                    <div class="card" id="ib-detail" style="display:none;margin-top:16px">
                        <div class="card-header">
                            <span id="ib-detail-title">图片详情</span>
                            <button class="btn btn-danger" style="float:right" onclick="ibCloseDetail()">关闭</button>
                        </div>
                        <div class="card-body">
                            <div id="ib-images" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px"></div>
                        </div>
                    </div>
                ''',
                js_content='''
                    let ibCurrentTag = null;

                    async function loadImageBotView() {
                        await ibRefresh();
                    }

                    async function ibRefresh() {
                        const el = document.getElementById('ib-list');
                        if (!el) return;
                        el.innerHTML = '<p style="color:var(--tx-t)">加载中...</p>';
                        try {
                            const resp = await fetch('/ImageBot/api/entries/list', {
                                headers: { 'Authorization': 'Bearer ' + localStorage.getItem('__ep_tk__') }
                            });
                            const data = await resp.json();
                            if (!data.entries || data.entries.length === 0) {
                                el.innerHTML = '<p style="color:var(--tx-t)">暂无词条</p>';
                                return;
                            }
                            let html = '';
                            data.entries.forEach(item => {
                                const uploaderList = Object.entries(item.uploaders || {})
                                    .map(([uid, cnt]) => escapeHtml(uid) + '(' + cnt + ')').join(', ');
                                html += `
                                    <div class="ib-item" style="padding:8px;margin-bottom:4px;background:var(--bg-t);border:1px solid var(--bd);border-radius:4px;cursor:pointer" data-tag="${escapeHtml(item.tag)}">
                                        <div style="display:flex;justify-content:space-between;align-items:center">
                                            <span style="font-weight:500;color:var(--tx-p)">${escapeHtml(item.tag)}</span>
                                            <span style="color:var(--tx-s);font-size:12px">${item.count}张</span>
                                        </div>
                                        <div style="color:var(--tx-t);font-size:11px;margin-top:2px">${uploaderList}</div>
                                    </div>
                                `;
                            });
                            el.innerHTML = html;
                            document.querySelectorAll('.ib-item').forEach(item => {
                                item.addEventListener('click', () => ibShowImages(item.dataset.tag));
                            });
                        } catch (e) {
                            el.innerHTML = '<p style="color:var(--er-c)">加载失败: ' + e.message + '</p>';
                        }
                    }

                    async function ibAdd() {
                        const tag = document.getElementById('ib-tag-input').value.trim();
                        const url = document.getElementById('ib-url-input').value.trim();
                        if (!tag || !url) { alert('请填写完整'); return; }
                        try {
                            const resp = await fetch('/ImageBot/api/entries/add', {
                                method: 'POST',
                                headers: {
                                    'Authorization': 'Bearer ' + localStorage.getItem('__ep_tk__'),
                                    'Content-Type': 'application/json'
                                },
                                body: JSON.stringify({ tag, url })
                            });
                            const data = await resp.json();
                            if (data.success) {
                                alert('添加成功');
                                document.getElementById('ib-tag-input').value = '';
                                document.getElementById('ib-url-input').value = '';
                                ibRefresh();
                            } else {
                                alert('失败: ' + (data.error || '未知错误'));
                            }
                        } catch (e) {
                            alert('失败: ' + e.message);
                        }
                    }

                    async function ibShowImages(tag) {
                        ibCurrentTag = tag;
                        const detail = document.getElementById('ib-detail');
                        const title = document.getElementById('ib-detail-title');
                        const grid = document.getElementById('ib-images');
                        detail.style.display = 'block';
                        title.textContent = '「' + tag + '」的图片';
                        grid.innerHTML = '<p style="color:var(--tx-t);grid-column:1/-1">加载中...</p>';
                        try {
                            const resp = await fetch('/ImageBot/api/entries/detail?tag=' + encodeURIComponent(tag), {
                                headers: { 'Authorization': 'Bearer ' + localStorage.getItem('__ep_tk__') }
                            });
                            const data = await resp.json();
                            const images = data.images || [];
                            if (images.length === 0) {
                                grid.innerHTML = '<p style="color:var(--tx-t);grid-column:1/-1">暂无图片</p>';
                                return;
                            }
                            let html = '';
                            images.forEach((img, idx) => {
                                html += '<div style="position:relative;border:1px solid var(--bd);border-radius:4px;overflow:hidden;background:var(--bg-p)">' +
                                    '<img src="' + escapeHtml(img.url) + '" style="width:100%;height:140px;object-fit:cover;display:block" ' +
                                    'onerror="this.style.display=\'none\';this.parentNode.innerHTML+=\'<div style=\"padding:8px;color:var(--tx-t);font-size:11px;word-break:break-all\">' + escapeHtml(img.url) + '</div>\'">' +
                                    '<button class="btn btn-danger" style="position:absolute;top:4px;right:4px;padding:2px 6px;font-size:11px" onclick="ibDeleteImage(' + idx + ')">删</button>' +
                                    '<div style="padding:4px 6px;color:var(--tx-s);font-size:10px">' + escapeHtml(img.uploader) + '</div>' +
                                    '</div>';
                            });
                            grid.innerHTML = html;
                        } catch (e) {
                            grid.innerHTML = '<p style="color:var(--er-c);grid-column:1/-1">加载失败: ' + e.message + '</p>';
                        }
                    }

                    async function ibDeleteImage(index) {
                        if (!ibCurrentTag || !confirm('确认删除这张图片吗？')) return;
                        try {
                            const resp = await fetch('/ImageBot/api/entries/images/delete', {
                                method: 'POST',
                                headers: {
                                    'Authorization': 'Bearer ' + localStorage.getItem('__ep_tk__'),
                                    'Content-Type': 'application/json'
                                },
                                body: JSON.stringify({ tag: ibCurrentTag, index })
                            });
                            const data = await resp.json();
                            if (data.success) {
                                ibShowImages(ibCurrentTag);
                                ibRefresh();
                            } else {
                                alert('删除失败: ' + (data.error || '未知错误'));
                            }
                        } catch (e) {
                            alert('删除失败: ' + e.message);
                        }
                    }

                    function ibCloseDetail() {
                        document.getElementById('ib-detail').style.display = 'none';
                        ibCurrentTag = null;
                    }

                    function ibFilter() {
                        const search = document.getElementById('ib-search').value.toLowerCase();
                        document.querySelectorAll('.ib-item').forEach(item => {
                            item.style.display = item.dataset.tag.toLowerCase().includes(search) ? 'block' : 'none';
                        });
                    }

                    function escapeHtml(text) {
                        const div = document.createElement('div');
                        div.textContent = text == null ? '' : String(text);
                        return div.innerHTML;
                    }
                ''',
                loader="loadImageBotView",
                group="group_tools",
            )
        except Exception as e:
            self.logger.warning(f"注册 Dashboard 视窗失败: {e}")
