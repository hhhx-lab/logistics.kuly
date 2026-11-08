from uuid import uuid4

import pytest
from sqlalchemy import select

from app.languages import LANGUAGES, language_reply, requested_language
from app.documents.preprocessing import assess_language
from app.workflows.translation import translation_language_defects


def test_explicit_target_and_language_reply_do_not_consume_questions():
    assert requested_language('把英文发票翻译成法语') == 'fr'
    assert requested_language('translate French into Spanish') == 'es'
    assert requested_language('翻译成英语和俄语') is None
    assert language_reply('法语。') == 'fr'
    assert language_reply('英语是什么意思？') is None
    assert language_reply('这份俄语单据是什么？') is None


@pytest.mark.parametrize('code,text', [
    ('zh-CN', '商业发票，买方收到十箱货物'),
    ('en', 'The seller delivers goods to the buyer.'),
    ('fr', 'La facture indique les marchandises et la quantité.'),
    ('es', 'La factura indica las mercancías y la cantidad.'),
    ('ru', 'Счёт содержит сведения о товаре и получателе.'),
])
def test_source_language_and_same_language_are_supported(code, text):
    assessment = assess_language(text)
    assert assessment.language == code and not assessment.uncertain
    assert not translation_language_defects(text, text, code)
    if code in {'fr', 'es', 'ru'}:
        assert 'source_copied' in translation_language_defects('The seller delivers goods to the buyer.', 'The seller delivers goods to the buyer.', code)


async def test_all_interface_languages_roundtrip_without_commit():
    from app.config import get_settings
    from app.identity.models import User
    from app.identity.seed import TENANT_A
    from app.identity.service import Principal
    from app.infra.database import create_database_engine, create_session_factory, tenant_transaction
    from app.preferences import service
    engine = create_database_engine(get_settings())
    try:
        async with create_session_factory(engine)() as db, tenant_transaction(db, TENANT_A):
            user = (await db.execute(select(User).where(User.account == 'demo'))).scalar_one()
            principal = Principal(user.id, TENANT_A, user.display_name, tuple(user.permissions), uuid4(), '')
            for code in LANGUAGES:
                before = await service.get_preferences(db, principal)
                await service.save_preferences(db, principal, service.PreferencesBody(interface_language=code, timezone='Asia/Shanghai', panel_width=640, motion='system', expected_version=before.version if before else 0), str(uuid4()))
                assert (await service.get_preferences(db, principal)).interface_language == code
            await db.rollback()
    finally:
        await engine.dispose()
