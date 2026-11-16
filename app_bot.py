"""Бот с календарём, компактной навигацией и подпиской на несколько групп."""
from __future__ import annotations

import os

from bot import MultiBot, save_state, KEYBOARD_VERSION, LOG
from navigation import NavigationMixin
from watch_groups import WatchGroupsMixin
from miniapp_bot import MiniAppMixin


RELEASE_VERSION = "2026-11-subject-alerts-v1"
RELEASE_TEXT = (
    "✨ Бот обновлён!\n\n"
    "🔔 В настройках появился раздел «Уведомления по предметам». "
    "Можно отключить оповещения, например для военной кафедры. "
    "Выбор личный и действует для всех ваших отслеживаемых групп со следующей проверки.\n\n"
    "📅 По воскресеньям ежедневное расписание больше не отправляется. "
    "Рассылка возобновляется в понедельник в выбранное вами время."
)

MINIAPP_RELEASE_VERSION = "2026-11-miniapp-v1"
MINIAPP_RELEASE_TEXT = (
    "В боте появилось Mini App с расписанием ДГТУ.\n\n"
    "Откройте его через меню «Расписание» или кнопку в настройках бота. "
    "Есть день, неделя, месяц, отдельные блоки групп и переход к расписанию преподавателя с фото.\n\n"
    "Двойное нажатие по паре открывает историю изменений. "
    "Журнал ведётся с момента установки обновления; старые события задним числом не восстанавливаются.\n\n"
    "Все привычные кнопки, команды и уведомления в чате продолжают работать."
)


class ScheduleBot(MiniAppMixin, NavigationMixin, WatchGroupsMixin, MultiBot):
    def new_profile(self, channel: str, chat_id: int | None = None, legacy: bool = False) -> dict:
        profile = super().new_profile(channel, chat_id, legacy)
        # Новые пользователи сразу видят новый интерфейс. Перенесённые получают анонс.
        if not legacy:
            profile["last_release_notified"] = (MINIAPP_RELEASE_VERSION if os.getenv("MINIAPP_URL", "").strip()
                                                 else RELEASE_VERSION)
        return profile

    def queue_release_notifications(self) -> bool:
        if self.miniapp_url and not self._miniapp_ready:
            return False
        changed = False
        version = MINIAPP_RELEASE_VERSION if self.miniapp_url else RELEASE_VERSION
        text = MINIAPP_RELEASE_TEXT if self.miniapp_url else RELEASE_TEXT
        for profile in self.active_profiles():
            if (profile.get("channel") != "telegram" or profile.get("owner_chat_id")
                    or profile.get("onboarding") or not profile.get("group_name")
                    or profile.get("last_release_notified") == version):
                continue
            if any(item.get("kind") == "release" and item.get("release_version") == version
                   for item in profile["pending"]):
                continue
            profile["pending"].insert(0, {"kind": "release", "text": text,
                                          "release_version": version})
            changed = True
        if changed:
            save_state(self.state_file, self.state)
        return changed

    def queue_due(self, now) -> bool:
        notices = self.queue_release_notifications()
        return super().queue_due(now) or notices

    def ensure_telegram_keyboards(self) -> None:
        # Анонс установит новую клавиатуру своим сообщением без второго уведомления.
        for profile in self.active_profiles():
            if (profile.get("channel") == "telegram" and not profile.get("onboarding")
                    and profile.get("group_name") and profile.get("keyboard_version") != KEYBOARD_VERSION
                    and not any(item.get("kind") == "release" for item in profile["pending"])):
                try:
                    self.send_profile_message(profile, "Кнопки расписания: Сегодня, Неделя, Месяц и Настройки.")
                except Exception as exc:
                    LOG.warning("Не удалось обновить кнопки чату %s: %s", profile.get("chat_id"), exc)
