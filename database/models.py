# START OF FILE database/models.py

"""
SQLAlchemy ORM модели для всех таблиц базы данных.

Эти модели используются Alembic для автоматической генерации миграций
и могут использоваться в будущем для ORM-запросов.
"""

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean, DateTime,
    ForeignKey, Index, UniqueConstraint, ARRAY, text
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


class User(Base):
    """Таблица пользователей бота."""
    __tablename__ = 'users'

    user_id = Column(BigInteger, primary_key=True)
    full_name = Column(Text, nullable=False)
    username = Column(Text, nullable=True)
    is_admin = Column(Boolean, default=False)
    is_vip = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now())
    default_project_id = Column(Text, nullable=True)
    default_server_id = Column(Text, nullable=True)
    default_question_type = Column(Text, nullable=True)

    # Relationships
    usages = relationship("UserUsage", back_populates="user", cascade="all, delete-orphan")
    message_history = relationship("MessageHistory", back_populates="user", cascade="all, delete-orphan")
    court_cases = relationship("CourtCase", back_populates="user", cascade="all, delete-orphan")
    complaints = relationship("ComplaintsHistory", back_populates="user", cascade="all, delete-orphan")
    assistant_cases = relationship("AssistantCase", back_populates="user", cascade="all, delete-orphan")
    reports = relationship("Report", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("UserSubscription", back_populates="user", cascade="all, delete-orphan")


class UserUsage(Base):
    """Таблица отслеживания суточных лимитов запросов."""
    __tablename__ = 'user_usage'

    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'), primary_key=True)
    usage_date = Column(DateTime, server_default=func.current_date(), primary_key=True)
    question_reqs = Column(Integer, default=0)
    detailed_reqs = Column(Integer, default=0)
    brief_reqs = Column(Integer, default=0)
    interview_gens = Column(Integer, default=0)
    complaint_gens = Column(Integer, default=0)

    user = relationship("User", back_populates="usages")


class Project(Base):
    """Справочник проектов (Majestic RP, GTA 5 RP и т.д.)."""
    __tablename__ = 'projects'

    project_id = Column(Text, primary_key=True)
    display_name = Column(Text, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    forum_base_url = Column(Text, nullable=True)
    is_disabled = Column(Boolean, default=False)

    servers = relationship("Server", back_populates="project", cascade="all, delete-orphan")


class Server(Base):
    """Справочник серверов."""
    __tablename__ = 'servers'

    server_id = Column(Text, primary_key=True)
    project_id = Column(Text, ForeignKey('projects.project_id', ondelete='CASCADE'), nullable=True)
    display_name = Column(Text, nullable=False)
    is_disabled = Column(Boolean, default=False)

    project = relationship("Project", back_populates="servers")
    law_documents = relationship("LawDocument", back_populates="server", cascade="all, delete-orphan")
    precedents = relationship("Precedent", back_populates="server", cascade="all, delete-orphan")


class LawDocument(Base):
    """Хранилище текстов законов и связанных URI файлов в Gemini."""
    __tablename__ = 'law_documents'

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(Text, ForeignKey('servers.server_id', ondelete='CASCADE'))
    source_thread_id = Column(Integer, nullable=True)
    source_url = Column(Text, nullable=True)
    title = Column(Text, nullable=False)
    content = Column(Text, nullable=True)
    gemini_file_uri = Column(Text, nullable=True)
    content_hash = Column(Text, nullable=True)
    derived_sync_pending = Column(
        Boolean,
        nullable=False,
        server_default=text('false'),
    )
    last_updated = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('server_id', 'source_thread_id', name='uq_law_documents_server_thread'),
        Index(
            'idx_law_documents_derived_sync_pending',
            'server_id',
            'source_thread_id',
            postgresql_where=text('derived_sync_pending'),
        ),
    )

    server = relationship("Server", back_populates="law_documents")


class MessageHistory(Base):
    """История диалогов с пользователями."""
    __tablename__ = 'message_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    server_id = Column(Text, nullable=True)
    role = Column(Text, nullable=False)
    content = Column(Text, nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="message_history")


class CourtCase(Base):
    """Судебные дела (темы с форума)."""
    __tablename__ = 'court_cases'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    server_name = Column(String(100), nullable=False)
    thread_url = Column(Text, unique=True, nullable=False)
    thread_id = Column(String(50), nullable=True)
    case_title = Column(Text, nullable=True)
    plaintiff = Column(String(200), nullable=True)
    defendant = Column(String(200), nullable=True)
    status = Column(String(20), default='active')
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)
    last_updated = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="court_cases")
    messages = relationship("CourtMessage", back_populates="court_case", cascade="all, delete-orphan")
    dialogue_logs = relationship("CourtDialogueLog", back_populates="court_case", cascade="all, delete-orphan")


class CourtMessage(Base):
    """Сообщения из судебного дела."""
    __tablename__ = 'court_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    court_case_id = Column(Integer, ForeignKey('court_cases.id', ondelete='CASCADE'))
    message_order = Column(Integer, nullable=False)
    author = Column(String(200), nullable=True)
    author_role = Column(String(20), nullable=True)
    message_timestamp = Column(DateTime(timezone=True), nullable=True)
    text_content = Column(Text, nullable=True)
    has_images = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    court_case = relationship("CourtCase", back_populates="messages")
    images = relationship("CourtImage", back_populates="court_message", cascade="all, delete-orphan")


class CourtImage(Base):
    """Изображения из судебных дел."""
    __tablename__ = 'court_images'

    id = Column(Integer, primary_key=True, autoincrement=True)
    court_message_id = Column(Integer, ForeignKey('court_messages.id', ondelete='CASCADE'))
    image_url = Column(Text, nullable=False)
    bbcode_tag = Column(Text, nullable=True)
    ocr_text = Column(Text, nullable=True)
    document_type = Column(String(50), nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    court_message = relationship("CourtMessage", back_populates="images")


class CourtDialogueLog(Base):
    """Логи диалогов с судебным помощником."""
    __tablename__ = 'court_dialogue_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    court_case_id = Column(Integer, ForeignKey('court_cases.id', ondelete='CASCADE'))
    user_message = Column(Text, nullable=False)
    assistant_response = Column(Text, nullable=False)
    used_tools = Column(ARRAY(Text), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    court_case = relationship("CourtCase", back_populates="dialogue_logs")


class ComplaintsHistory(Base):
    """История составленных жалоб и исков."""
    __tablename__ = 'complaints_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    server_id = Column(Text, nullable=True)
    complaint_type = Column(Text, nullable=False)
    forum_url = Column(Text, nullable=False)
    situation_description = Column(Text, nullable=False)
    generated_text = Column(Text, nullable=False)
    generated_bb_code = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="complaints")


class Precedent(Base):
    """Судебные прецеденты и толкования."""
    __tablename__ = 'precedents'

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(Text, ForeignKey('servers.server_id', ondelete='CASCADE'))
    source_url = Column(Text, nullable=False)
    source_thread_id = Column(Integer, nullable=True)
    title = Column(Text, nullable=False)
    full_content = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    gemini_file_uri = Column(Text, nullable=True)
    content_hash = Column(Text, nullable=True)
    image_urls = Column(ARRAY(Text), nullable=True)
    precedent_type = Column(Text, default='precedent')
    is_active = Column(Boolean, default=True)
    last_updated = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('server_id', 'source_thread_id', name='uq_precedents_server_thread'),
        Index('idx_precedents_server_active', 'server_id', 'is_active'),
    )

    server = relationship("Server", back_populates="precedents")


class AssistantCase(Base):
    """Дела ролевого помощника."""
    __tablename__ = 'assistant_cases'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    server_id = Column(Text, nullable=True)
    rp_role = Column(Text, nullable=True)
    case_stage = Column(Text, nullable=True)
    status = Column(String(20), default='active')
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_updated_at = Column(DateTime(timezone=True), server_default=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="assistant_cases")
    messages = relationship("AssistantMessage", back_populates="case", cascade="all, delete-orphan")
    digest = relationship(
        "AssistantCaseDigest", back_populates="case", uselist=False, cascade="all, delete-orphan"
    )


class AssistantMessage(Base):
    """Сообщения в делах ролевого помощника."""
    __tablename__ = 'assistant_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey('assistant_cases.id', ondelete='CASCADE'))
    sender_role = Column(Text, nullable=False)
    content = Column(Text, nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    case = relationship("AssistantCase", back_populates="messages")


class AssistantCaseDigest(Base):
    """Карта дела: сжатая сводка старой части переписки.

    Производный кэш. Покрывает сообщения вплоть до covered_message_id, всё, что новее,
    подаётся модели сырым. Обновляется инкрементально (прошлая карта + новые сообщения),
    поэтому факты из начала дела не теряются при обрезке истории.
    """
    __tablename__ = 'assistant_case_digests'

    case_id = Column(Integer, ForeignKey('assistant_cases.id', ondelete='CASCADE'), primary_key=True)
    digest = Column(Text, nullable=False)
    covered_message_id = Column(Integer, nullable=False)
    covered_count = Column(Integer, nullable=False, server_default='0')
    source_chars = Column(Integer, nullable=False, server_default='0')
    model = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    case = relationship("AssistantCase", back_populates="digest")


class Report(Base):
    """Репорты пользователей."""
    __tablename__ = 'reports'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    status = Column(String(20), default='open')
    category = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closed_by = Column(BigInteger, nullable=True)

    user = relationship("User", back_populates="reports")
    messages = relationship("ReportMessage", back_populates="report", cascade="all, delete-orphan")


class ReportMessage(Base):
    """Сообщения в репортах."""
    __tablename__ = 'report_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(Integer, ForeignKey('reports.id', ondelete='CASCADE'))
    sender_id = Column(BigInteger, nullable=False)
    sender_role = Column(String(20), nullable=False)  # 'user' или 'admin'
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    report = relationship("Report", back_populates="messages")


class UserSubscription(Base):
    """Подписки пользователей (бонусы)."""
    __tablename__ = 'user_subscriptions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    subscription_type = Column(Text, nullable=False)
    activated_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="subscriptions")


class AssistantUsage(Base):
    """Использование лимитов ролевого помощника."""
    __tablename__ = 'assistant_usage'

    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'), primary_key=True)
    usage_type = Column(Text, primary_key=True)
    last_reset = Column(DateTime(timezone=True), server_default=func.now())
    count = Column(Integer, default=0)


class LoopholeJob(Base):
    """Очередь задач анализатора дыр в законке.

    Каждая запись — один запрос на анализ. Основная очередь обрабатывается
    одним воркером (concurrency из config.LOOPHOLE_QUEUE_CONCURRENCY), VIP/админы
    имеют приоритет, админ может запустить задачу out-of-band (параллельно очереди).
    """
    __tablename__ = 'loophole_jobs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    # queued | running | done | failed | cancelled
    status = Column(Text, nullable=False, server_default='queued')
    priority = Column(Integer, nullable=False, server_default='0')  # больше = выше
    params = Column(JSONB, nullable=False)                          # снимок параметров on_run
    is_out_of_band = Column(Boolean, nullable=False, server_default='false')
    manual_position = Column(Integer, nullable=True)               # ручной порядок от админа
    position_msg_id = Column(BigInteger, nullable=True)            # message_id сообщения «вы в очереди»
    attempts = Column(Integer, nullable=False, server_default='0')  # счётчик попыток выполнения
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # БД-гарантия требования «1 активный запрос на пользователя» (защита от double-tap).
        Index('uq_loophole_active_user', 'user_id', unique=True,
              postgresql_where=text("status IN ('queued','running') AND is_out_of_band = false")),
        # Индекс для выборки следующей задачи и подсчёта позиций.
        Index('idx_loophole_queue_order', 'status', 'is_out_of_band',
              'priority', 'manual_position', 'created_at'),
    )


# END OF FILE database/models.py

