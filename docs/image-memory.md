# 图片上下文与开发模式

- CHAT_PERSONA_ENABLED=false：不加载聊天人设文件；原提示词保留，恢复时改为 true 并重启后端。
- 图片直接送入主聊天模型，不再默认先调用独立视觉模型。
- IMAGE_HISTORY_TURNS=5：当前轮及前四轮带原图；聚合消息算一轮，工具调用不增加轮数。
- 第六轮开始，待归档图片使用 IMAGE_ARCHIVE_MODEL 单独生成一次客观描述。一次批量最多八张，不附带完整聊天历史。
- 当前归档模型为 google/gemini-2.5-flash-lite，共用现有 OpenRouter URL 和 key。主聊天及工具回看仍使用 OPENROUTER_CHAT_MODEL。
- IMAGE_ARCHIVE_EXTRA_BODY 可单独配置归档请求参数（默认为空），不继承主聊天模型的专用参数；归档失败不会自动切换到昂贵的主模型。
- 归档失败不会清除原图或中断聊天，五分钟后可重试；尚无描述时明确标记，可用工具回看。
- 原图放在 data/media/images/，SQLite image_assets 只保存文件引用、哈希、所属会话/消息/轮次和描述，不存 Base64。
- 发送到 API 时按需转换为图片内容块；大图最多缩至 2048 像素长边，存储原件不变。单次输入最多八张、单张源文件最多 20 MiB。
- view_image(image_id) 取回真实图片内容；search_images(query, limit, offset) 搜索本会话目录，空查询可以翻页。
- 回看结果只用于当前推理，不重复写用户消息，不产生额外 assistant 记录，不刷新图片原始轮次。
- 图片索引、描述和独立轮次计数不依赖历史文本窗口。裁剪/未来 compact 后仍可检索；用户显式删除一轮或清空会话会删除相应图片引用。
- 当前仍使用原有文本 token 预算裁剪，未额外引入自动文本摘要器。上下文带最近十二张图片的简短索引，更早图片通过工具检索。
- 工具最多三轮，最多四次看图；工具链共享 210 秒预算，图片归档最多 45 秒，与现有 Gateway 300 秒请求超时协调。
- 新入站图片自动登记。此前仅保存临时路径的图片不会凭空恢复或自动重新调用模型补档。
- /health 的 chat 字段显示人设开关、图片窗口和回看能力。

验证：

    .\venv\Scripts\python.exe -m pytest -q tests/unit/test_image_memory.py
    .\venv\Scripts\python.exe scripts/verify-image-memory-live.py --live

第二条使用生成的小图片、临时数据库和当前聊天 API，验证直接识图、归档及裁剪后回看；不发送 Telegram 消息，不修改真实会话。
