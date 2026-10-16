"""Small real-provider probe, isolated from chat history and Telegram delivery."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

from PIL import Image, ImageDraw, ImageFont
from app import config
from app.schemas import MediaAttachment
from app.services import chat_service
from app.services.chat import image_memory
from app.storage import db, image_store


def main() -> None:
    if '--live' not in sys.argv:
        raise SystemExit('Pass --live to make a few small requests to the configured chat provider.')
    with tempfile.TemporaryDirectory(prefix='neno-image-check-') as directory:
        root = Path(directory)
        db.DB_DIR, db.DB_PATH = root / 'data', root / 'data' / 'bot.db'
        config.IMAGE_LOCAL_SPOOL_ROOTS = (root.resolve(),)
        db.init_db()
        path = root / 'test.png'
        canvas = Image.new('RGB', (240, 120), 'white')
        font_path = Path('C:/Windows/Fonts/arial.ttf')
        font = ImageFont.truetype(str(font_path), 56) if font_path.exists() else ImageFont.load_default(size=56)
        ImageDraw.Draw(canvas).text((65, 20), '42', fill='black', font=font)
        canvas.save(path)
        session = 'isolated-image-check'
        attachment = image_memory.prepare_attachments(session, [MediaAttachment(
            kind='image', media_path=str(path)
        )], '测试图片')[0]
        first = chat_service.run_chat_turn(session, '图片上的数字是什么？只回答数字。', user_records=[{
            'content': '图片上的数字是什么？只回答数字。', 'message_type': 'image',
            'metadata': {'attachments': [attachment.model_dump()]},
        }])
        print(json.dumps({'stage': 'direct_image', 'reply': first['reply'][:120]}, ensure_ascii=False), flush=True)
        if '42' not in first['reply']:
            raise RuntimeError('Direct image verification did not recover the fixture digits')
        image_store.complete_turn(session, 5)
        image_memory.archive_older_images(session, 6, 'verify-image-archive')
        asset = image_store.get_image(session, attachment.image_id)
        print(json.dumps({'stage': 'archive', 'ready': asset['archive_state'] == 'ready',
                          'description_chars': len(asset['description'])}), flush=True)
        if asset['archive_state'] != 'ready':
            raise RuntimeError('Live archive request did not produce a saved description')
        db.execute_write('DELETE FROM messages WHERE session_id=?', (session,))
        called = []
        original = image_memory.execute_image_tool

        def tracked(session_id, name, arguments):
            called.append(name)
            return original(session_id, name, arguments)

        image_memory.execute_image_tool = tracked
        try:
            second = chat_service.run_chat_turn(session,
                f'请调用 view_image 重新核对图片 {attachment.image_id} 上的数字，不要仅根据旧描述作答。最终只回答数字。')
        finally:
            image_memory.execute_image_tool = original
        count = len(db.get_session_messages(session))
        print(json.dumps({'stage': 'recall_after_compact', 'tools': called,
                          'reply': second['reply'][:120], 'database_message_count': count}, ensure_ascii=False), flush=True)
        if 'view_image' not in called or '42' not in second['reply'] or count != 2:
            raise RuntimeError('Live recall verification failed')
        print(json.dumps({'status': 'PASS', 'persona_enabled': config.CHAT_PERSONA_ENABLED,
                          'persona_prompt_chars': len(config.SYSTEM_PROMPT),
                          'telegram_messages_sent': 0, 'live_history_modified': False}), flush=True)


if __name__ == '__main__':
    main()
