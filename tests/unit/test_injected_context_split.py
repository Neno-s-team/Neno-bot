from app.services.chat_service import INJECTED_CONTEXT_PREFIX, split_injected_context


def test_split_strips_injected_block():
    message = f"{INJECTED_CONTEXT_PREFIX}名字共2个：\n喵、惊\n\n在吗"
    block, clean = split_injected_context(message)
    assert block.startswith(INJECTED_CONTEXT_PREFIX)
    assert clean == "在吗"


def test_split_passes_plain_text_through():
    block, clean = split_injected_context("就是普通消息")
    assert block == ""
    assert clean == "就是普通消息"


def test_split_block_only_without_body():
    block, clean = split_injected_context(f"{INJECTED_CONTEXT_PREFIX}只有块")
    assert block == f"{INJECTED_CONTEXT_PREFIX}只有块"
    assert clean == ""
